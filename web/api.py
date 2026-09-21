"""API HTTP sobre el snapshot. Stateless, sin red, sin base de datos.

    uvicorn web.api:app --reload

El proceso levanta UN `Motor` en el arranque y lo deja caliente. Todos los
endpoints leen de esa instancia; ninguno escribe ni sale a internet, asi que
la latencia es la del calculo (20-30 ms) y no la de un tercero.

Si el snapshot no esta, el proceso NO arranca. Es deliberado: una web que
levanta sin datos y devuelve 500 en cada consulta es mas dificil de
diagnosticar que una que se niega a arrancar diciendo que falta el ETL.
"""

from __future__ import annotations

import io
import math
import os
import unicodedata
from urllib.parse import quote
from contextlib import asynccontextmanager

import numpy as np
import pandas as pd
from fastapi import FastAPI, HTTPException, Query, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, JSONResponse

from agroenso import consulta, snapshot

AQUI = os.path.dirname(os.path.abspath(__file__))
ESTATICO = os.path.join(AQUI, "static")

# Cache de resultados. La estadistica es determinista: mismo snapshot y
# mismos parametros dan siempre lo mismo, asi que repetir el calculo no
# aporta nada. Se invalida sola al reiniciar el proceso, que es exactamente
# cuando cambia el snapshot.
_cache: dict = {}
CACHE_MAX = 512

MOTOR: consulta.Motor | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global MOTOR
    MOTOR = consulta.Motor(os.environ.get("AGROENSO_SNAPSHOT"))
    n = len(MOTOR.rindes)
    print(f"snapshot cargado: {n:,} filas, {len(MOTOR.partidos)} partidos, "
          f"clima={'si' if MOTOR.tiene_clima else 'no'}", flush=True)
    yield
    MOTOR = None


app = FastAPI(
    title="agroenso",
    description="Rinde por partido cruzado contra la fase ENSO, sobre la serie "
                "historica del MAGYP y el ONI de NOAA CPC.",
    version="2.0",
    lifespan=lifespan,
)

# Sin cookies ni sesion: la API es publica y de solo lectura, asi que un
# origen abierto no expone nada. Si en algun momento se agrega login, esto
# tiene que pasar a una lista blanca.
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["GET"],
                   allow_headers=["*"])

# El geojson de departamentos son ~500 KB de texto plano y comprime a menos de
# un tercio. Sin esto, cada visita al mapa se baja medio mega.
app.add_middleware(GZipMiddleware, minimum_size=1024)


# ---------------------------------------------------------------------------
# Serializacion
# ---------------------------------------------------------------------------

def _limpio(v):
    """Valor apto para JSON. NaN e Inf no son JSON valido y hay que anularlos."""
    if v is None:
        return None
    if isinstance(v, (np.integer,)):
        return int(v)
    if isinstance(v, (np.floating, float)):
        f = float(v)
        return None if not math.isfinite(f) else f
    if isinstance(v, (np.bool_, bool)):
        return bool(v)
    if isinstance(v, pd.Timestamp):
        return v.date().isoformat()
    if v is pd.NA or (isinstance(v, float) and math.isnan(v)):
        return None
    try:
        if pd.isna(v):
            return None
    except (TypeError, ValueError):
        pass
    return str(v) if not isinstance(v, (int, str)) else v


def registros(df: pd.DataFrame) -> list[dict]:
    """DataFrame -> lista de dicts JSON-safe."""
    if df is None or df.empty:
        return []
    return [{k: _limpio(v) for k, v in fila.items()}
            for fila in df.to_dict("records")]


def _motor() -> consulta.Motor:
    if MOTOR is None:
        raise HTTPException(503, "El snapshot todavia no termino de cargar")
    return MOTOR


def _cacheado(clave, fn):
    if clave in _cache:
        return _cache[clave]
    v = fn()
    if len(_cache) >= CACHE_MAX:
        _cache.clear()
    _cache[clave] = v
    return v


def _csv(df: pd.DataFrame, nombre: str) -> Response:
    """CSV con `;` y coma decimal: es lo que abre Excel en es-AR sin pelear."""
    buf = io.StringIO()
    df.to_csv(buf, index=False, sep=";", decimal=",")
    return Response(
        buf.getvalue().encode("utf-8-sig"), media_type="text/csv",
        headers={"Content-Disposition":
                 f'attachment; filename="{_ascii(nombre)}"; '
                 f"filename*=UTF-8''{quote(nombre)}"})


def _ascii(nombre: str) -> str:
    """Nombre de archivo sin acentos, para el `filename` clasico.

    Los headers HTTP son latin-1: un "Salliquelo" con tilde rompe la
    respuesta entera. El nombre con acentos viaja aparte en `filename*`
    (RFC 5987), que es lo que lee cualquier navegador actual.
    """
    base = unicodedata.normalize("NFKD", nombre)
    base = "".join(c for c in base if not unicodedata.combining(c))
    return "".join(c if c.isalnum() or c in "._-" else "_" for c in base)


# ---------------------------------------------------------------------------
# Metadatos y ENSO
# ---------------------------------------------------------------------------

@app.get("/api/salud", tags=["meta"])
def salud():
    m = _motor()
    return {"ok": True, "filas": len(m.rindes), "partidos": len(m.partidos),
            "clima": m.tiene_clima, "construido": m.meta.get("construido")}


@app.get("/api/meta", tags=["meta"])
def meta():
    """Procedencia del dato. Un informe de riesgo sin fecha de corte no sirve."""
    m = _motor()
    return {**m.meta, "clima_disponible": m.tiene_clima}


@app.get("/api/enso", tags=["enso"])
def enso_actual():
    return _limpio_dict(_motor().enso_actual())


def _limpio_dict(d: dict) -> dict:
    out = {}
    for k, v in d.items():
        if isinstance(v, list):
            out[k] = [{kk: _limpio(vv) for kk, vv in x.items()}
                      if isinstance(x, dict) else _limpio(x) for x in v]
        elif isinstance(v, dict):
            out[k] = _limpio_dict(v)
        else:
            out[k] = _limpio(v)
    return out


@app.get("/api/enso/fases", tags=["enso"])
def enso_fases(ciclo: str = Query("verano", pattern="^(verano|invierno)$"),
               desde: int = Query(1980, ge=1950, le=2100)):
    """Clasificacion de cada campania. `ciclo` cambia que trimestres ONI se
    promedian: SON-OND-NDJ-DJF para verano, JJA-JAS-ASO-SON para invierno."""
    return registros(_motor().fases_campania(ciclo, desde))


# ---------------------------------------------------------------------------
# Catalogos
# ---------------------------------------------------------------------------

@app.get("/api/provincias", tags=["catalogo"])
def provincias():
    p = _motor().partidos
    g = (p.groupby("provincia").agg(partidos=("departamento_id", "nunique"))
         .reset_index().sort_values("provincia"))
    return registros(g)


@app.get("/api/partidos", tags=["catalogo"])
def partidos(q: str | None = None, provincia: str | None = None,
             cultivo: str | None = None, limite: int = Query(600, ge=1, le=2000)):
    """Partidos con serie de rinde. Filtrable por texto, provincia o cultivo."""
    m = _motor()
    p = m.partidos
    if cultivo:
        try:
            cn = m.normalizar_cultivo(cultivo)
        except consulta.SinDatos as e:
            raise HTTPException(404, str(e))
        con = {dep for (dep, cul) in m._idx if cul == cn}
        p = p[p["departamento_id"].isin(con)]
    if provincia:
        from agroenso.rindes import normalizar
        pn = normalizar(provincia)
        p = p[p["provincia"].map(normalizar) == pn]
    if q:
        from agroenso.rindes import normalizar
        qn = normalizar(q)
        p = p[p["departamento"].map(normalizar).str.contains(qn, na=False)]
    return registros(p.head(limite))


@app.get("/api/cultivos", tags=["catalogo"])
def cultivos_(departamento_id: str | None = None,
              minimo: int = Query(10, ge=1, le=60)):
    """Cultivos disponibles. Con `departamento_id`, solo los de ese partido.

    `ventana_fenologica=false` marca los cultivos sin periodo critico
    definido (yerba, citrus, cania): se pueden cruzar contra la fase ENSO
    igual, pero no tienen clima ni ranking de sensibilidad.
    """
    m = _motor()
    if departamento_id:
        d = m.cultivos_de(departamento_id, minimo)
        if d.empty:
            raise HTTPException(404, f"Sin cultivos con {minimo}+ campanias "
                                     f"en {departamento_id}")
        return registros(d)
    if m.catalogo is not None:
        return registros(m.catalogo.sort_values("filas", ascending=False))
    return registros(m.pares_disponibles(minimo))


@app.get("/api/geo/departamentos", tags=["catalogo"])
def geo_departamentos():
    """Poligonos de los partidos con serie de rinde, en GeoJSON.

    Solo trae `id` en las propiedades: el nombre y la provincia salen de
    `/api/partidos`, que el frontend ya tiene cargado. Se cachea fuerte porque
    los limites departamentales cambian cada varios anios, no cada mes.
    """
    ruta = snapshot.ruta_geojson(_motor().raiz)
    if not os.path.exists(ruta):
        raise HTTPException(503, "El snapshot no incluye geometrias. "
                                 "Reconstruirlo con: python -m etl.construir")
    return FileResponse(ruta, media_type="application/geo+json",
                        headers={"Cache-Control": "public, max-age=86400"})


# ---------------------------------------------------------------------------
# Analisis
# ---------------------------------------------------------------------------

def _analisis(m, departamento_id, cultivo, desde, hasta, umbral, tendencia):
    clave = ("an", departamento_id, cultivo, desde, hasta, umbral, tendencia)
    try:
        return _cacheado(clave, lambda: m.analizar(
            departamento_id, cultivo, desde=desde, hasta=hasta,
            umbral=umbral, metodo_tendencia=tendencia))
    except consulta.SinDatos as e:
        raise HTTPException(404, str(e))
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.get("/api/analisis", tags=["analisis"])
def analisis(departamento_id: str,
             cultivo: str,
             desde: int = Query(1980, ge=1970, le=2024),
             hasta: int | None = Query(None, ge=1970, le=2100),
             umbral: float = Query(-15.0, ge=-90, le=0,
                                   description="desvio %% bajo el cual la "
                                               "campania cuenta como siniestro"),
             tendencia: str = Query("movil", pattern="^(movil|lineal)$")):
    """Informe completo de un partido x cultivo.

    Devuelve la tabla campania a campania (rinde, tendencia tecnologica,
    desvio y fase ENSO), la estadistica por fase con IC 90% por bootstrap, y
    el ranking de sensibilidad climatica si el snapshot tiene clima.
    """
    m = _motor()
    r = _analisis(m, departamento_id, cultivo, desde, hasta, umbral, tendencia)
    return {
        "partido": r["partido"],
        "cultivo": r["cultivo"],
        "ciclo": r["ciclo"],
        "parametros": r["parametros"],
        "con_clima": r["con_clima"],
        "campanias": registros(r["campanias"]),
        "resumen_fase": registros(r["resumen_fase"]),
        "sensibilidad": registros(r["sensibilidad"]),
        "advertencias": _advertencias(r),
        "fuente": {"rinde": "MAGYP estimaciones agricolas (partido, no lote)",
                   "enso": "ONI, NOAA CPC",
                   "corte": _motor().meta.get("construido")},
    }


def _advertencias(r: dict) -> list[str]:
    """Lo que el numero solo no dice y el usuario necesita leer.

    El README del proyecto es explicito en que el ENSO explica una fraccion
    de la varianza y que con n chico el IC cruza el cero seguido. Eso tiene
    que viajar con el dato, no quedar en un README que nadie abre.
    """
    av = []
    res = r["resumen_fase"]
    if not len(res):
        return ["Sin campanias clasificables en el periodo elegido."]
    flojas = res[res["campanias"] < 10]["fase"].tolist()
    if flojas:
        av.append(f"Fases con menos de 10 campanias ({', '.join(flojas)}): "
                  f"la mediana se apoya en pocos casos, leer el IC antes que "
                  f"el punto.")
    cruzan = res[(res["ic90_inf"] < 0) & (res["ic90_sup"] > 0)]["fase"].tolist()
    if cruzan:
        av.append(f"El IC 90% de la diferencia de medianas cruza el cero en "
                  f"{', '.join(cruzan)}: con esta serie no se puede afirmar "
                  f"que la fase mueva el rinde medio.")
    if not r["con_clima"]:
        av.append("Sin variables climaticas en este snapshot: el cruce ENSO x "
                  "rinde es completo, pero no hay ranking de sensibilidad.")
    av.append("El rinde es de PARTIDO, no de lote: promedia manejos, suelos y "
              "fechas de siembra, y subestima la varianza de un lote puntual.")
    return av


@app.get("/api/analisis.csv", tags=["analisis"])
def analisis_csv(departamento_id: str, cultivo: str,
                 desde: int = Query(1980, ge=1970, le=2024),
                 hasta: int | None = None,
                 umbral: float = Query(-15.0, ge=-90, le=0),
                 tendencia: str = Query("movil", pattern="^(movil|lineal)$"),
                 tabla: str = Query("campanias",
                                    pattern="^(campanias|resumen|sensibilidad)$")):
    """La misma consulta, descargable. Es el formato en que esto se usa de verdad."""
    m = _motor()
    r = _analisis(m, departamento_id, cultivo, desde, hasta, umbral, tendencia)
    df = {"campanias": r["campanias"], "resumen": r["resumen_fase"],
          "sensibilidad": r["sensibilidad"]}[tabla]
    etiqueta = f"{r['partido']['departamento']}_{r['cultivo']}".replace(" ", "_")
    return _csv(df, f"{tabla}_{etiqueta}.csv")


@app.get("/api/ranking", tags=["analisis"])
def ranking(cultivo: str,
            fase: str = Query("Nina", pattern="^(Nino|Nina|Neutro)$"),
            desde: int = Query(1980, ge=1970, le=2024),
            umbral: float = Query(-15.0, ge=-90, le=0),
            tendencia: str = Query("movil", pattern="^(movil|lineal)$"),
            minimo_campanias: int = Query(15, ge=5, le=55),
            superficie_minima_ha: float = Query(0.0, ge=0),
            provincia: str | None = None,
            limite: int = Query(60, ge=1, le=600)):
    """Que partidos sufren mas una fase, para un cultivo. La vista de informe.

    Ordena por desvio mediano de rinde en esa fase. No corre bootstrap por
    partido (serian millones de remuestreos por request): para el intervalo
    de confianza de un partido concreto hay que abrir `/api/analisis`.
    """
    m = _motor()
    clave = ("rk", cultivo, fase, desde, umbral, tendencia, minimo_campanias,
             superficie_minima_ha)
    try:
        d = _cacheado(clave, lambda: m.ranking(
            cultivo, fase=fase, desde=desde, umbral=umbral,
            metodo_tendencia=tendencia, minimo_campanias=minimo_campanias,
            superficie_minima_ha=superficie_minima_ha))
    except consulta.SinDatos as e:
        raise HTTPException(404, str(e))
    except ValueError as e:
        raise HTTPException(400, str(e))
    if provincia:
        from agroenso.rindes import normalizar
        pn = normalizar(provincia)
        d = d[d["provincia"].map(normalizar) == pn]
    # `mapa` lleva TODOS los partidos evaluados, no solo el top: el ranking se
    # lee de a 25 pero el mapa los pinta a todos. Van en arrays posicionales
    # [id, desvio, frec_siniestro, n_campanias] en vez de objetos con claves
    # repetidas: son ~500 filas y asi el payload baja de 90 KB a 20.
    mapa = [[r.departamento_id, r.desvio_mediano_pct, r.frec_siniestro_pct,
             int(r.campanias_fase)] for r in d.itertuples()]
    return {
        "cultivo": cultivo, "fase": fase,
        "parametros": {"desde": desde, "umbral_siniestro_pct": umbral,
                       "metodo_tendencia": tendencia,
                       "minimo_campanias": minimo_campanias,
                       "superficie_minima_ha": superficie_minima_ha},
        "partidos_evaluados": int(len(d)),
        "ranking": registros(d.head(limite)),
        "mapa_campos": ["departamento_id", "desvio_mediano_pct",
                        "frec_siniestro_pct", "campanias_fase"],
        "mapa": mapa,
        "nota": "Ordenado por desvio mediano en la fase. Sin IC por partido: "
                "abrir /api/analisis para el intervalo de confianza.",
    }


# ---------------------------------------------------------------------------
# Frontend
# ---------------------------------------------------------------------------

@app.get("/", include_in_schema=False)
def raiz():
    idx = os.path.join(ESTATICO, "index.html")
    if not os.path.exists(idx):
        return JSONResponse({"agroenso": "API viva", "docs": "/docs"})
    return FileResponse(idx)


if os.path.isdir(ESTATICO):
    from fastapi.staticfiles import StaticFiles
    app.mount("/static", StaticFiles(directory=ESTATICO), name="static")
