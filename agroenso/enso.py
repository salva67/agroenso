"""Indices ENSO desde NOAA CPC.

Dos series, ambas texto plano sin credenciales:

  oni.ascii.txt   ONI (Oceanic Nino Index): anomalia de SST en Nino 3.4 en
                  trimestres moviles (DJF..NDJ), base movil de 30 anios.
                  Desde 1950. Es el indice oficial para declarar Nino/Nina.
  sstoi.indices   Anomalias mensuales de Nino 1+2, 3, 4 y 3.4. Desde 1982.

Convencion de anio en el ONI: la etiqueta es el anio del mes CENTRAL del
trimestre. DJF 1950 = dic 1949 + ene 1950 + feb 1950.
"""

from __future__ import annotations

import io

import pandas as pd

# `requests` se importa DENTRO de las funciones que salen a la red, no aca
# arriba. La imagen de la web no lo instala a proposito, y la cadena de
# imports de `web.api` pasa por este modulo: con el import a nivel de modulo,
# el proceso web no arrancaba. Asi la regla "la web no toca la red" deja de
# ser una promesa del README y pasa a estar sostenida por la estructura: si
# alguien mete una descarga en el camino de lectura, revienta al llamarla con
# un ImportError que dice exactamente que paso.
from . import cache

URL_ONI = "https://www.cpc.ncep.noaa.gov/data/indices/oni.ascii.txt"
URL_SSTOI = "https://www.cpc.ncep.noaa.gov/data/indices/sstoi.indices"
TIMEOUT = 90
DIAS_CACHE = 7

TRIMESTRES = ["DJF", "JFM", "FMA", "MAM", "AMJ", "MJJ", "JJA",
              "JAS", "ASO", "SON", "OND", "NDJ"]

# Mes central de cada trimestre movil (1-12), para ubicarlo en el calendario.
MES_CENTRAL = {t: i + 1 for i, t in enumerate(TRIMESTRES)}

UMBRAL = 0.5  # |ONI| >= 0.5 define fase segun CPC


def _bajar(url: str) -> str:
    import requests

    txt = cache.leer_json(url, DIAS_CACHE)
    if txt is None:
        r = requests.get(url, timeout=TIMEOUT)
        r.raise_for_status()
        txt = r.text
        cache.guardar_json(url, txt)
    return txt


def serie_oni() -> pd.DataFrame:
    """DataFrame con columnas: anio, trimestre, sst, oni, mes_central."""
    df = pd.read_csv(io.StringIO(_bajar(URL_ONI)), sep=r"\s+")
    df.columns = ["trimestre", "anio", "sst", "oni"]
    df["anio"] = df["anio"].astype(int)
    df["oni"] = df["oni"].astype(float)
    df["mes_central"] = df["trimestre"].map(MES_CENTRAL)
    return df


def serie_nino34_mensual() -> pd.DataFrame:
    """DataFrame con columnas: anio, mes, nino34_sst, nino34_anom."""
    df = pd.read_csv(io.StringIO(_bajar(URL_SSTOI)), sep=r"\s+")
    df = df.rename(columns={"YR": "anio", "MON": "mes"})
    # El archivo repite el nombre ANOM para cada region; pandas las desambigua.
    col = [c for c in df.columns if c.startswith("NINO3.4")][0]
    anom = df.columns[list(df.columns).index(col) + 1]
    out = df[["anio", "mes", col, anom]].copy()
    out.columns = ["anio", "mes", "nino34_sst", "nino34_anom"]
    return out.astype({"anio": int, "mes": int})


def clasificar(oni: float) -> str:
    if pd.isna(oni):
        return "sin dato"
    if oni >= UMBRAL:
        return "Nino"
    if oni <= -UMBRAL:
        return "Nina"
    return "Neutro"


def intensidad(oni: float) -> str:
    """Escala habitual del CPC sobre el |ONI| del pico."""
    if pd.isna(oni):
        return "sin dato"
    a = abs(oni)
    if a < UMBRAL:
        return "neutro"
    if a < 1.0:
        return "debil"
    if a < 1.5:
        return "moderado"
    if a < 2.0:
        return "fuerte"
    return "muy fuerte"


def indice_oni(df_oni: pd.DataFrame | None = None) -> dict:
    """Mapa {(trimestre, anio): oni} para lookup O(1).

    `fases_por_campania` consulta 4 trimestres por campania y se llama una vez
    por partido. Filtrando el DataFrame cada vez son ~180 escaneos completos
    por partido y el costo domina el pipeline; con el diccionario armado una
    sola vez pasa a ser aritmetica.
    """
    d = serie_oni() if df_oni is None else df_oni
    return dict(zip(zip(d["trimestre"], d["anio"].astype(int)),
                    d["oni"].astype(float)))


def _oni_de(df: pd.DataFrame, trimestre: str, anio_etiqueta: int) -> float:
    s = df[(df.trimestre == trimestre) & (df.anio == anio_etiqueta)]["oni"]
    return float(s.iloc[0]) if len(s) else float("nan")


def fases_por_campania(
    anios: list[int] | range,
    trimestres: tuple[str, ...] = ("SON", "OND", "NDJ", "DJF"),
    df_oni: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """Clasifica cada campania de verano por el ONI de su ventana.

    `anio` es el anio de SIEMBRA (campania 2019/2020 -> anio 2019).
    Por defecto promedia SON-OND-NDJ-DJF, que cubre la implantacion y el
    periodo critico de los cultivos de verano en la pampa humeda.

    Devuelve: anio, campania, oni_medio, oni_pico, fase, intensidad y una
    columna oni_<trimestre> por cada trimestre de la ventana.
    """
    idx = indice_oni(df_oni)
    filas = []
    for a in anios:
        vals = {}
        for t in trimestres:
            # Si el mes central del trimestre es dic o posterior a set, el
            # trimestre pertenece al anio de siembra; si es ene/feb, al anio
            # siguiente. Regla: etiqueta = anio + 1 cuando el mes central
            # cae en el primer semestre.
            etiqueta = a + 1 if MES_CENTRAL[t] <= 6 else a
            vals[f"oni_{t}"] = idx.get((t, etiqueta), float("nan"))
        serie = pd.Series(vals).dropna()
        medio = float(serie.mean()) if len(serie) else float("nan")
        pico = float(serie.loc[serie.abs().idxmax()]) if len(serie) else float("nan")
        filas.append({
            "anio": a,
            "campania": f"{a}/{a + 1}",
            **vals,
            "oni_medio": medio,
            "oni_pico": pico,
            "fase": clasificar(medio),
            "intensidad": intensidad(pico) if clasificar(medio) != "Neutro" else "neutro",
        })
    return pd.DataFrame(filas)


def estado_actual() -> dict:
    """Ultimo dato observado del ENSO y su tendencia reciente.

    Sirve para campanias todavia abiertas, donde la ventana de trimestres que
    usa `fases_por_campania` aun no fue publicada por el CPC. Devuelve lo
    OBSERVADO, no un pronostico: la extrapolacion es del usuario, o del
    boletin del CPC / IRI.
    """
    o = serie_oni()
    m = serie_nino34_mensual()
    u = o.iloc[-1]
    um = m.iloc[-1]
    ult6 = o["oni"].tail(6).to_numpy()
    tendencia = "subiendo" if ult6[-1] > ult6[0] else (
        "bajando" if ult6[-1] < ult6[0] else "estable")
    return {
        "oni_trimestre": str(u.trimestre),
        "oni_anio": int(u.anio),
        "oni": float(u.oni),
        "fase_observada": clasificar(float(u.oni)),
        "intensidad_observada": intensidad(float(u.oni)),
        "tendencia_6_trimestres": tendencia,
        "nino34_anio": int(um.anio),
        "nino34_mes": int(um.mes),
        "nino34_anom": float(um.nino34_anom),
        "ultimos_oni": o.tail(6)[["trimestre", "anio", "oni"]].to_dict("records"),
    }


def cobertura_ventana(anio: int, trimestres: tuple[str, ...]) -> dict:
    """Cuantos trimestres de la ventana de una campania ya estan publicados.

    Con 0 de 4 la campania no se puede clasificar todavia; el analisis
    historico sigue valiendo, pero la fase de la campania en curso hay que
    asumirla explicitamente.
    """
    df = serie_oni()
    hay = []
    for t in trimestres:
        etiqueta = anio + 1 if MES_CENTRAL[t] <= 6 else anio
        v = _oni_de(df, t, etiqueta)
        if not pd.isna(v):
            hay.append(t)
    return {"anio": anio, "trimestres": list(trimestres),
            "publicados": hay, "n": len(hay), "de": len(trimestres),
            "completa": len(hay) == len(trimestres)}


def fases_por_campania_invierno(
    anios: list[int] | range,
    trimestres: tuple[str, ...] = ("JJA", "JAS", "ASO", "SON"),
    df_oni: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """Idem para trigo/cebada: la campania se siembra y cosecha en el mismo
    anio calendario, y la ventana critica es la primavera."""
    return fases_por_campania(anios, trimestres, df_oni)
