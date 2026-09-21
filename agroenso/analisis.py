"""Cruce ENSO x clima x rinde y estadisticos de riesgo por fase.

El nucleo metodologico es el detrend. El rinde de cualquier partido de la
pampa sube ~1-2% por anio por genetica y manejo: si se comparan rindes
crudos entre fases ENSO, se termina midiendo en que decada cayo cada fase,
no el efecto del clima. Todo se analiza sobre el DESVIO respecto de la
tendencia tecnologica.
"""

from __future__ import annotations

import datetime as dt

import numpy as np
import pandas as pd

from . import clima, cultivos, enso, rindes


# ---------------------------------------------------------------------------
# Tendencia tecnologica
# ---------------------------------------------------------------------------

def tendencia(anios: np.ndarray, y: np.ndarray, metodo: str = "movil",
              ventana: int = 11) -> np.ndarray:
    """Tendencia tecnologica del rinde. Devuelve el valor esperado por anio.

    'lineal'  regresion OLS sobre el anio. Simple y estable, pero asume una
              tasa de mejora constante en 50 anios, cosa que no es cierta.
    'movil'   media movil centrada de `ventana` anios, con los bordes
              completados por regresion lineal sobre los extremos. Sigue los
              quiebres tecnologicos (siembra directa, RR, biotecnologia) a
              costa de absorber parte del efecto climatico si hay rachas
              largas de una misma fase.
    """
    anios = np.asarray(anios, dtype=float)
    y = np.asarray(y, dtype=float)
    ok = ~np.isnan(y)
    if ok.sum() < 5:
        return np.full(len(y), np.nan)

    if metodo == "lineal":
        b, a = np.polyfit(anios[ok], y[ok], 1)
        return b * anios + a

    if metodo != "movil":
        raise ValueError("metodo desconocido: " + str(metodo))

    # Reindexar a anios completos para que la ventana movil sea temporal y no
    # posicional: si faltan campanias, una movil posicional las ignora y
    # deforma la tendencia.
    s = pd.Series(y[ok], index=anios[ok].astype(int), dtype=float)
    s = s[~s.index.duplicated()]
    completo = s.reindex(range(int(s.index.min()), int(s.index.max()) + 1))
    suave = completo.rolling(ventana, center=True,
                             min_periods=max(3, ventana // 2)).mean()
    # `copy=True` NO es redundante. Desde pandas 3 rige Copy-on-Write y
    # `to_numpy()` devuelve una VISTA de solo lectura del buffer de la Serie
    # en vez de una copia; el relleno de bordes de abajo escribe sobre este
    # array y muere con "assignment destination is read-only". En pandas 2 el
    # mismo codigo andaba, asi que el bug solo aparecia al construir la imagen
    # con la version nueva.
    val = suave.to_numpy(dtype=float, copy=True)
    idx = np.asarray(completo.index, dtype=float)

    # Bordes: la movil centrada no cubre los primeros ni los ultimos anios.
    # Se extrapolan con la pendiente local del tramo valido mas cercano.
    validos = np.where(~np.isnan(val))[0]
    if len(validos) >= 2:
        for lado in ("ini", "fin"):
            tramo = (validos[:min(ventana, len(validos))] if lado == "ini"
                     else validos[-min(ventana, len(validos)):])
            if len(tramo) < 2:
                continue
            b, a = np.polyfit(idx[tramo], val[tramo], 1)
            objetivo = (idx < idx[tramo[0]]) if lado == "ini" else (idx > idx[tramo[-1]])
            hueco = objetivo & np.isnan(val)
            val[hueco] = b * idx[hueco] + a

    return (pd.Series(val, index=completo.index)
            .reindex(pd.Index(anios.astype(int)))
            .to_numpy(dtype=float))


# ---------------------------------------------------------------------------
# Armado de la tabla campania x campania
# ---------------------------------------------------------------------------

def tabla_campanias(lat: float, lon: float, cultivo: str,
                    departamento_id: str | None = None,
                    partido: str | None = None, provincia: str | None = None,
                    desde: int = 1980, hasta: int | None = None,
                    metodo_tendencia: str = "movil", con_clima: bool = True,
                    df_rindes: pd.DataFrame | None = None) -> pd.DataFrame:
    """Una fila por campania con fase ENSO, clima de la ventana critica,
    rinde observado, tendencia y desvio.

    `con_clima=False` saltea Open-Meteo y devuelve solo ENSO x rinde. Sirve
    para carteras grandes: el limite horario de la API de clima es lo unico
    lento del pipeline, y la comparacion de rinde por fase no lo necesita.
    """
    ficha = cultivos.ficha(cultivo)
    hasta = hasta or dt.date.today().year

    r = rindes.serie(cultivo, partido=partido, provincia=provincia,
                     departamento_id=departamento_id, df=df_rindes)
    if r.empty:
        raise RuntimeError(
            "Sin datos de rinde para cultivo=" + repr(cultivo)
            + " partido=" + repr(partido) + " depto_id=" + repr(departamento_id))
    r = r.dropna(subset=["anio"])
    r["anio"] = r["anio"].astype(int)
    r = r[(r.anio >= desde) & (r.anio <= hasta)].copy()

    anios = sorted(r["anio"].unique())
    # La etiqueta de campania la manda el MAGYP; la del modulo enso se
    # descarta para no duplicar columna en el merge.
    f = enso.fases_por_campania(anios, trimestres=ficha["enso"]).drop(columns=["campania"])

    if not con_clima:
        t = r.merge(f, on="anio", how="left")
        t["rinde_tendencia"] = tendencia(t["anio"].to_numpy(float),
                                         t["rendimiento_kgxha"].to_numpy(float),
                                         metodo_tendencia)
        t["desvio_kg"] = t["rendimiento_kgxha"] - t["rinde_tendencia"]
        t["desvio_pct"] = 100 * t["desvio_kg"] / t["rinde_tendencia"]
        t["perdida_cosecha_pct"] = 100 * (
            1 - t["superficie_cosechada_ha"] / t["superficie_sembrada_ha"])
        return t

    # Una sola descarga de clima que cubra todas las ventanas del periodo.
    ini_glob = min(cultivos.ventana(a, ficha["ciclo_completo"])[0] for a in anios)
    fin_glob = max(cultivos.ventana(a, ficha["ciclo_completo"])[1] for a in anios)
    fin_glob = min(fin_glob, dt.date.today() - dt.timedelta(days=6))
    diaria = clima.serie_diaria(lat, lon, ini_glob, fin_glob)

    filas = []
    for a in anios:
        ci, cf = cultivos.ventana(a, ficha["critica"])
        ti, tf = cultivos.ventana(a, ficha["ciclo_completo"])
        crit = clima.agregar_ventana(diaria, ci, cf)
        tot = clima.agregar_ventana(diaria, ti, tf)
        # Campania en curso o incompleta: la ventana critica no cerro.
        if not crit or crit.get("dias", 0) <= (cf - ci).days:
            continue
        fila = {"anio": a, "vc_ini": ci, "vc_fin": cf}
        fila.update({"vc_" + k: v for k, v in crit.items() if k != "dias"})
        fila["cc_pp_mm"] = tot.get("pp_mm")
        fila["cc_balance_mm"] = tot.get("balance_mm")
        filas.append(fila)
    c = pd.DataFrame(filas)

    t = r.merge(f, on="anio", how="left")
    if not c.empty:
        t = t.merge(c, on="anio", how="left")
    t["rinde_tendencia"] = tendencia(t["anio"].to_numpy(float),
                                     t["rendimiento_kgxha"].to_numpy(float),
                                     metodo_tendencia)
    t["desvio_kg"] = t["rendimiento_kgxha"] - t["rinde_tendencia"]
    t["desvio_pct"] = 100 * t["desvio_kg"] / t["rinde_tendencia"]
    t["perdida_cosecha_pct"] = 100 * (
        1 - t["superficie_cosechada_ha"] / t["superficie_sembrada_ha"])
    return t


# ---------------------------------------------------------------------------
# Estadisticos por fase
# ---------------------------------------------------------------------------

def _boot_dif_medianas(d: np.ndarray, resto: np.ndarray, iteraciones: int,
                       rng: np.random.Generator) -> np.ndarray:
    """Bootstrap de la diferencia de medianas, todas las iteraciones de una.

    Es el mismo remuestreo con reposicion de siempre, armado como dos
    matrices (iteraciones x n) en vez de un loop: la mediana por filas la
    resuelve numpy. Con 4000 iteraciones y 3 fases el loop en Python costaba
    ~780 ms por partido, que era el grueso del tiempo de toda la consulta.

    OJO: cambia el ORDEN en que se consumen los numeros aleatorios, asi que
    con la misma semilla los IC difieren en la ultima cifra respecto de la
    version anterior. Es ruido de remuestreo, no un cambio de metodo.
    """
    a = rng.choice(d, size=(iteraciones, len(d)), replace=True)
    b = rng.choice(resto, size=(iteraciones, len(resto)), replace=True)
    return np.median(a, axis=1) - np.median(b, axis=1)


def resumen_por_fase(t: pd.DataFrame, umbral_siniestro: float = -15.0,
                     iteraciones: int = 4000, semilla: int = 7) -> pd.DataFrame:
    """Estadistica de riesgo por fase ENSO.

    `umbral_siniestro` es el desvio porcentual bajo el cual se considera que
    la campania fue mala. -15% es un default razonable para un multirriesgo
    de rinde; conviene ajustarlo al deducible real de la poliza.
    """
    rng = np.random.default_rng(semilla)
    base = t.dropna(subset=["desvio_pct"])
    todas = base["desvio_pct"].to_numpy()
    fases = base["fase"].to_numpy()
    out = []
    for fase in ["Nino", "Neutro", "Nina"]:
        g = base[base["fase"] == fase]
        d = g["desvio_pct"].to_numpy()
        if len(d) == 0:
            continue
        malas = int((d <= umbral_siniestro).sum())
        # Bootstrap de la diferencia de medianas contra el resto de las
        # campanias. Con 15-20 casos por fase el ruido es grande y conviene
        # que el intervalo lo diga, en vez de reportar una media pelada.
        resto = todas[fases != fase]
        if len(resto) >= 3 and len(d) >= 3:
            difs = _boot_dif_medianas(d, resto, iteraciones, rng)
        else:
            difs = np.array([np.nan])
        fila = {
            "fase": fase,
            "campanias": len(d),
            "desvio_mediano_pct": round(float(np.median(d)), 1),
            "desvio_medio_pct": round(float(np.mean(d)), 1),
            "p10_pct": round(float(np.percentile(d, 10)), 1),
            "p90_pct": round(float(np.percentile(d, 90)), 1),
            "frec_siniestro_pct": round(100 * malas / len(d), 1),
            "siniestros": malas,
            "dif_mediana_vs_resto": round(float(np.median(difs)), 1),
            "ic90_inf": round(float(np.percentile(difs, 5)), 1),
            "ic90_sup": round(float(np.percentile(difs, 95)), 1),
        }
        if "vc_pp_mm" in g:
            fila["pp_critica_mediana_mm"] = round(float(g["vc_pp_mm"].median()), 1)
            fila["racha_seca_mediana_dias"] = round(
                float(g["vc_racha_seca_max"].median()), 1)
        out.append(fila)
    return pd.DataFrame(out)


PREDICTORES = {
    "vc_pp_mm": "lluvia en ventana critica (mm)",
    "vc_balance_mm": "balance pp - et0 en ventana critica (mm)",
    "vc_racha_seca_max": "racha seca maxima en ventana critica (dias)",
    "vc_dias_tmax_33": "dias con tmax >= 33 en ventana critica",
    "vc_dias_tmax_35": "dias con tmax >= 35 en ventana critica",
    "cc_pp_mm": "lluvia del ciclo completo, barbecho incluido (mm)",
    "cc_balance_mm": "balance pp - et0 del ciclo completo (mm)",
    "oni_medio": "ONI promedio de la ventana ENSO",
}


def sensibilidad(t: pd.DataFrame, minimo: int = 8) -> pd.DataFrame:
    """Ranking de variables climaticas por su poder explicativo del desvio.

    Regresion simple de cada candidata contra el desvio de rinde. No es un
    modelo: es un tamiz para ver cual de las variables merece entrar en uno,
    y para no quedarse con la lluvia de la ventana critica por costumbre
    cuando el agua almacenada en el barbecho explica mas.
    """
    out = []
    for col, etiqueta in PREDICTORES.items():
        if col not in t:
            continue
        d = t.dropna(subset=["desvio_pct", col])
        if len(d) < minimo:
            continue
        x = d[col].to_numpy(float)
        y = d["desvio_pct"].to_numpy(float)
        if np.ptp(x) == 0:
            continue
        b, a = np.polyfit(x, y, 1)
        r = float(np.corrcoef(x, y)[0, 1])
        out.append({
            "variable": col,
            "descripcion": etiqueta,
            "n": int(len(d)),
            "r_pearson": round(r, 3),
            "r2": round(r * r, 3),
            "pendiente_pct_por_unidad": round(float(b), 3),
            "umbral_desvio_cero": round(float(-a / b), 1) if b else None,
        })
    cols = ["variable", "descripcion", "n", "r_pearson", "r2",
            "pendiente_pct_por_unidad", "umbral_desvio_cero"]
    if not out:
        # Sin clima en la tabla no hay ninguna candidata y el DataFrame sale
        # sin columnas: ordenar por "r2" ahi tira KeyError. Devolver el
        # esquema vacio deja que el consumidor pregunte `len()` y siga.
        return pd.DataFrame(columns=cols)
    return (pd.DataFrame(out)
            .sort_values("r2", ascending=False)
            .reset_index(drop=True))


def sensibilidad_lluvia(t: pd.DataFrame) -> dict:
    """Compatibilidad: solo la lluvia de la ventana critica."""
    s = sensibilidad(t)
    fila = s[s.variable == "vc_pp_mm"]
    return fila.iloc[0].to_dict() if len(fila) else {}
