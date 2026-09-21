"""Lectura del padron de polizas y evaluacion de cada una contra el historico.

El archivo de entrada puede ser CSV o XLSX. Los nombres de columna se
reconocen por alias, asi que no hace falta renombrar nada a mano: alcanza
con que exista, para cada poliza, un identificador, un cultivo y una
ubicacion (coordenadas del lote, o el partido).
"""

from __future__ import annotations

import os

import numpy as np
import pandas as pd
import requests

from . import analisis, geo, rindes

ALIAS = {
    "poliza": ["poliza", "nro_poliza", "numero_poliza", "n_poliza", "id_poliza",
               "item", "id", "certificado"],
    "asegurado": ["asegurado", "cliente", "tomador", "razon_social", "productor"],
    "cultivo": ["cultivo", "especie", "producto"],
    "segunda": ["2a_siembra", "2_siembra", "segunda_siembra", "segunda"],
    "lat": ["lat", "latitud", "latitude", "y"],
    "lon": ["lon", "lng", "long", "longitud", "longitude", "x"],
    "partido": ["partido", "departamento", "depto", "distrito"],
    "localidad": ["localidad", "campo", "establecimiento"],
    "provincia": ["provincia", "prov"],
    "departamento_id": ["departamento_id", "indec", "codigo_indec", "id_depto"],
    "campania": ["campania", "campana", "cosecha", "ciclo"],
    "estado": ["estado", "situacion", "vigencia"],
    "superficie_ha": ["superficie_ha", "superficie", "hectareas", "has", "ha",
                      "sup_asegurada", "superficie_asegurada"],
    # OJO: en padrones multi-moneda, "suma asegurada" viene en la moneda de
    # cada poliza y NO se puede sumar. Por eso se leen tambien la moneda y las
    # columnas de capital por moneda, y la cartera se pondera por superficie.
    "suma_asegurada": ["suma_asegurada", "capital_asegurado", "capital", "suma"],
    "moneda": ["moneda", "divisa"],
    "capital_ars": ["capital_ars", "suma_ars", "capital_pesos"],
    "capital_usd": ["capital_usd", "suma_usd", "capital_dolares"],
    "rinde_garantizado": ["rinde_garantizado", "rinde_garantia", "rinde_asegurado",
                          "garantia_kg", "rinde_kgxha"],
    "prima": ["prima", "premio", "prima_bruta"],
}

# Los padrones nombran los cultivos en comercial ("TRIGO"); el MAGYP usa
# categorias propias ("trigo total"). Sin este puente no empalma nada.
CULTIVO_MAGYP = {
    "trigo": "trigo total",
    "trigo pan": "trigo total",
    "soja": "soja total",
    "maiz": "maiz",
    "girasol": "girasol",
    "cebada": "cebada total",
    "sorgo": "sorgo",
    "avena": "avena",
    "centeno": "centeno",
    "colza": "colza",
    "mani": "mani",
    "arveja": "arveja",
    "lenteja": "lenteja",
    "alpiste": "alpiste",
    "lino": "lino",
    "mijo": "mijo",
}


def cultivo_magyp(nombre: str, segunda: bool = False) -> str:
    """Traduce el cultivo del padron al vocabulario del MAGYP.

    La soja de segunda tiene otra ventana critica y otro techo de rinde que
    la de primera, asi que se separa cuando el padron lo informa.
    """
    c = rindes.normalizar(nombre)
    if c in ("soja", "soja total") and segunda:
        return "soja 2da"
    return CULTIVO_MAGYP.get(c, c)


def _mapa_columnas(cols) -> dict:
    inv = {}
    for c in cols:
        inv[rindes.normalizar(c).replace(" ", "_")] = c
    salida = {}
    for canonico, opciones in ALIAS.items():
        for o in opciones:
            if o in inv:
                salida[canonico] = inv[o]
                break
    return salida


def leer(ruta: str, hoja: str | int | None = None,
         campania: str | None = None, estado: str | None = None) -> pd.DataFrame:
    """Carga el padron y normaliza los nombres de columna a los canonicos.

    `campania` y `estado` filtran por igualdad, ignorando mayusculas y
    acentos (asi "2026-2027" / "vigente" andan sin importar como venga).
    """
    ext = os.path.splitext(ruta)[1].lower()
    if ext in (".xlsx", ".xlsm", ".xls"):
        df = pd.read_excel(ruta, sheet_name=hoja if hoja is not None else 0)
    else:
        # Los exports de sistemas argentinos suelen venir con ; y coma decimal.
        df = pd.read_csv(ruta, sep=None, engine="python", encoding="utf-8-sig")
    mapa = _mapa_columnas(df.columns)
    faltan = [k for k in ("poliza", "cultivo") if k not in mapa]
    if faltan:
        raise ValueError(
            "Al padron le faltan columnas obligatorias: " + ", ".join(faltan)
            + ". Columnas encontradas: " + ", ".join(map(str, df.columns))
            + ". Alias reconocidos en agroenso/polizas.py -> ALIAS.")
    if not ({"lat", "lon"} <= set(mapa)) and "partido" not in mapa and \
            "departamento_id" not in mapa:
        raise ValueError(
            "El padron no trae ubicacion. Se necesita lat+lon del lote, "
            "o el partido, o el departamento_id INDEC.")
    out = df.rename(columns={v: k for k, v in mapa.items()})
    for c in ("lat", "lon", "superficie_ha", "suma_asegurada", "capital_ars",
              "capital_usd", "rinde_garantizado", "prima"):
        if c in out:
            out[c] = pd.to_numeric(
                out[c].astype(str).str.replace(".", "", regex=False)
                .str.replace(",", ".", regex=False)
                if out[c].dtype == object else out[c], errors="coerce")
    if "departamento_id" in out:
        out["departamento_id"] = out["departamento_id"].astype(str).str.zfill(5)

    for campo, valor in (("campania", campania), ("estado", estado)):
        if valor is None:
            continue
        if campo not in out:
            raise ValueError(
                "No se puede filtrar por " + campo + ": el padron no trae esa columna")
        objetivo = rindes.normalizar(valor)
        out = out[out[campo].map(lambda v: rindes.normalizar(v)) == objetivo]
        if out.empty:
            raise ValueError(
                "Ningun registro con " + campo + "=" + repr(valor))
    return out.reset_index(drop=True)


def resolver_ubicacion(fila: pd.Series) -> dict:
    """Completa lat/lon y departamento_id para una poliza.

    Prioridad: coordenadas del lote (lo mejor) > departamento_id declarado >
    nombre del partido. Deja constancia de con que se resolvio, porque un
    analisis hecho sobre el centroide del partido no vale lo mismo que uno
    hecho sobre el lote.
    """
    lat, lon = fila.get("lat"), fila.get("lon")
    if pd.notna(lat) and pd.notna(lon):
        d = geo.departamento_de(float(lat), float(lon))
        if d:
            return {**d, "lat": float(lat), "lon": float(lon), "origen_geo": "lote"}
        return {"lat": float(lat), "lon": float(lon), "origen_geo": "lote sin depto"}

    dep_id = fila.get("departamento_id")
    nombre = fila.get("partido")
    prov = fila.get("provincia")
    c = geo.centroide(
        departamento_id=dep_id if pd.notna(dep_id) else None,
        nombre=nombre if pd.notna(nombre) else None,
        provincia=prov if pd.notna(prov) else None)
    if not c:
        return {"origen_geo": "sin resolver"}
    return {**c, "provincia": prov, "origen_geo": "centroide de partido"}


def _analizar(geoinfo: dict, cultivo: str, desde: int, metodo_tendencia: str,
              con_clima: bool, tab) -> tuple:
    """Analiza un lote, degradando a solo ENSO x rinde si el clima falla.

    Que Open-Meteo se quede sin cuota no puede sacar una poliza del analisis:
    el cruce ENSO x rinde no usa clima y da exactamente los mismos desvios.
    Se pierden las columnas climaticas de esa poliza, nada mas. Devuelve
    (tabla | None, aviso).
    """
    def correr(clima: bool):
        return analisis.tabla_campanias(
            geoinfo["lat"], geoinfo["lon"], cultivo,
            departamento_id=geoinfo["departamento_id"], desde=desde,
            metodo_tendencia=metodo_tendencia, con_clima=clima, df_rindes=tab)

    if con_clima:
        try:
            return correr(True), "ok"
        except (RuntimeError, requests.RequestException) as e:
            # Fallo del servicio de clima: se reintenta sin el.
            motivo = str(e)[:120]
            try:
                return correr(False), "ok, sin clima (" + motivo + ")"
            except (KeyError, RuntimeError, ValueError) as e2:
                return None, str(e2)[:200]
        except (KeyError, ValueError) as e:
            # Cultivo sin ventana definida o sin serie de rinde: no se
            # arregla sacando el clima.
            return None, str(e)[:200]
    try:
        return correr(False), "ok"
    except (KeyError, RuntimeError, ValueError) as e:
        return None, str(e)[:200]


def evaluar(ruta_padron: str, desde: int = 1980, umbral_siniestro: float = -15.0,
            metodo_tendencia: str = "movil", hoja: str | int | None = None,
            campania: str | None = None, estado: str | None = None,
            con_clima: bool = True,
            df_rindes: pd.DataFrame | None = None) -> tuple[pd.DataFrame, dict]:
    """Corre el analisis para cada poliza del padron.

    Devuelve (resumen por poliza y fase, detalle por poliza) donde el detalle
    es {clave: tabla campania x campania} para poder auditar cualquier fila.

    Varias polizas pueden compartir lote y cultivo. El analisis se cachea por
    (departamento, cultivo, celda ERA5) para no repetir el mismo calculo
    decenas de veces en carteras con muchos items del mismo campo.
    """
    padron = leer(ruta_padron, hoja=hoja, campania=campania, estado=estado)
    tab = rindes.tabla() if df_rindes is None else df_rindes
    filas, detalle, memo = [], {}, {}

    for i, p in padron.iterrows():
        geoinfo = resolver_ubicacion(p)
        segunda = rindes.normalizar(p.get("segunda", "")) in ("si", "s", "true", "1")
        cultivo = cultivo_magyp(str(p.get("cultivo", "")), segunda)
        base = {
            "poliza": p.get("poliza"),
            "asegurado": p.get("asegurado"),
            "cultivo_padron": p.get("cultivo"),
            "cultivo": cultivo,
            "localidad": p.get("localidad"),
            "superficie_ha": p.get("superficie_ha"),
            "moneda": p.get("moneda"),
            "suma_asegurada": p.get("suma_asegurada"),
            "capital_ars": p.get("capital_ars"),
            "capital_usd": p.get("capital_usd"),
            "partido": geoinfo.get("departamento") or p.get("partido"),
            "departamento_id": geoinfo.get("departamento_id"),
            "lat": geoinfo.get("lat"),
            "lon": geoinfo.get("lon"),
            "origen_geo": geoinfo.get("origen_geo"),
        }
        if not geoinfo.get("departamento_id") or geoinfo.get("lat") is None:
            filas.append({**base, "diagnostico": "sin ubicacion resoluble"})
            continue

        clave = (geoinfo["departamento_id"], cultivo,
                 round(geoinfo["lat"], 2), round(geoinfo["lon"], 2))
        if clave not in memo:
            memo[clave] = _analizar(
                geoinfo, cultivo, desde, metodo_tendencia, con_clima, tab)
        t, aviso = memo[clave]
        if t is None:
            filas.append({**base, "diagnostico": aviso})
            continue

        detalle[f"{p.get('poliza')}|{i}"] = t
        res = analisis.resumen_por_fase(t, umbral_siniestro=umbral_siniestro)
        for _, r in res.iterrows():
            filas.append({
                **base,
                "diagnostico": aviso,
                "campanias_analizadas": int(t["desvio_pct"].notna().sum()),
                "fase": r["fase"],
                "campanias_fase": r["campanias"],
                "desvio_mediano_pct": r["desvio_mediano_pct"],
                "p10_pct": r["p10_pct"],
                "frec_siniestro_pct": r["frec_siniestro_pct"],
                "dif_mediana_vs_resto": r["dif_mediana_vs_resto"],
                "ic90_inf": r["ic90_inf"],
                "ic90_sup": r["ic90_sup"],
            })
    return pd.DataFrame(filas), detalle


def cartera_por_fase(resumen: pd.DataFrame, ponderar: str = "superficie") -> pd.DataFrame:
    """Exposicion de la cartera bajo cada fase ENSO.

    Pondera la frecuencia de siniestro y el desvio esperado de cada poliza
    por su tamanio. Es una lectura de exposicion relativa, NO una prima: no
    hay curva de siniestralidad ni deducible en el calculo.

    Por defecto pondera por SUPERFICIE. Es deliberado: en un padron con
    polizas en pesos y en dolares, la suma asegurada no es comparable entre
    filas y sumarla da un numero sin sentido. Las hectareas siempre son
    hectareas. Los capitales se informan al costado, separados por moneda.
    """
    d = resumen[resumen["diagnostico"].astype(str).str.startswith("ok")].copy()
    if d.empty:
        return pd.DataFrame()

    if ponderar == "capital":
        d["peso"] = d["suma_asegurada"].fillna(0.0)
    elif ponderar == "superficie":
        d["peso"] = d["superficie_ha"].fillna(0.0)
    else:
        raise ValueError("ponderar debe ser 'superficie' o 'capital'")

    out = []
    for fase, g in d.groupby("fase"):
        w = g["peso"].to_numpy(float)
        tot = w.sum()
        if tot <= 0:
            w, tot = np.ones(len(g)), float(len(g))
        fila = {
            "fase": fase,
            "polizas": int(len(g)),
            "superficie_ha": round(float(g["superficie_ha"].fillna(0).sum()), 1),
            "frec_siniestro_pond_pct": round(
                float((g["frec_siniestro_pct"] * w).sum() / tot), 1),
            "desvio_mediano_pond_pct": round(
                float((g["desvio_mediano_pct"] * w).sum() / tot), 1),
            "p10_pond_pct": round(float((g["p10_pct"] * w).sum() / tot), 1),
        }
        for col in ("capital_ars", "capital_usd"):
            if col in g:
                fila[col] = round(float(g[col].fillna(0).sum()), 0)
        out.append(fila)
    orden = {"Nino": 0, "Neutro": 1, "Nina": 2}
    return (pd.DataFrame(out).sort_values("fase", key=lambda s: s.map(orden))
            .reset_index(drop=True))


def plantilla(ruta: str) -> str:
    """Escribe un CSV de ejemplo con el esquema minimo esperado."""
    ej = pd.DataFrame([
        {"poliza": "A-1001", "asegurado": "Ejemplo SA", "cultivo": "soja total",
         "lat": -36.7539, "lon": -62.9569, "partido": "", "provincia": "Buenos Aires",
         "campania": "2025/2026", "superficie_ha": 320, "suma_asegurada": 96000,
         "rinde_garantizado": 2200},
        {"poliza": "A-1002", "asegurado": "Ejemplo SA", "cultivo": "maiz",
         "lat": "", "lon": "", "partido": "Trenque Lauquen",
         "provincia": "Buenos Aires", "campania": "2025/2026",
         "superficie_ha": 180, "suma_asegurada": 72000, "rinde_garantizado": 6500},
    ])
    ej.to_csv(ruta, index=False, sep=";", decimal=",", encoding="utf-8-sig")
    return ruta
