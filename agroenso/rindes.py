"""Estimaciones agricolas del MAGYP (rinde, superficie y produccion por partido).

El dataset vive en el CKAN de datos abiertos. El nombre del CSV lleva la
fecha de publicacion, asi que NUNCA se hardcodea la URL: se consulta la API
del CKAN y se toma el recurso CSV mas reciente.

Cobertura: campanias 1969/70 en adelante, todos los partidos/departamentos
del pais, ~40 cultivos. Clave geografica: departamento_id = codigo INDEC de
5 digitos (2 de provincia + 3 de departamento).
"""

from __future__ import annotations

import os
import unicodedata

import pandas as pd

# `requests` se importa DENTRO de las funciones que salen a la red, no aca
# arriba. La imagen de la web no lo instala a proposito, y la cadena de
# imports de `web.api` pasa por este modulo: con el import a nivel de modulo,
# el proceso web no arrancaba. Asi la regla "la web no toca la red" deja de
# ser una promesa del README y pasa a estar sostenida por la estructura: si
# alguien mete una descarga en el camino de lectura, revienta al llamarla con
# un ImportError que dice exactamente que paso.
from . import cache

CKAN = "https://datos.magyp.gob.ar/api/3/action/package_show"
DATASET = "estimaciones-agricolas"
UA = "Mozilla/5.0 (compatible; agroenso/0.1; analisis de riesgo agricola)"
TIMEOUT = 300
DIAS_CACHE_META = 7
DIAS_CACHE_CSV = 30

COLUMNAS = ["cultivo", "anio", "campania", "provincia", "provincia_id",
            "departamento", "departamento_id", "superficie_sembrada_ha",
            "superficie_cosechada_ha", "produccion_tm", "rendimiento_kgxha"]


def normalizar(s: str) -> str:
    """Minusculas sin acentos, para comparar nombres de partidos y cultivos."""
    s = unicodedata.normalize("NFKD", str(s))
    s = "".join(c for c in s if not unicodedata.combining(c))
    return " ".join(s.lower().split())


def url_csv() -> str:
    """URL del recurso CSV mas reciente del dataset."""
    import requests

    meta = cache.leer_json(f"ckan|{DATASET}", DIAS_CACHE_META)
    if meta is None:
        r = requests.get(CKAN, params={"id": DATASET},
                         headers={"User-Agent": UA}, timeout=TIMEOUT)
        r.raise_for_status()
        meta = r.json()
        cache.guardar_json(f"ckan|{DATASET}", meta)
    recursos = [x for x in meta["result"]["resources"]
                if x.get("format", "").upper() == "CSV"
                and "estimaciones-agricolas" in x["url"]]
    if not recursos:
        raise RuntimeError("El CKAN del MAGYP no devolvio ningun CSV de estimaciones")
    recursos.sort(key=lambda x: x.get("last_modified") or x.get("created") or "")
    return recursos[-1]["url"]


def _descargar(url: str) -> str:
    import requests

    destino = cache.ruta_binaria(url, "csv")
    if cache.vigente(destino, DIAS_CACHE_CSV) and os.path.getsize(destino) > 1_000_000:
        return destino
    with requests.get(url, headers={"User-Agent": UA}, timeout=TIMEOUT, stream=True) as r:
        r.raise_for_status()
        with open(destino, "wb") as f:
            for bloque in r.iter_content(1 << 16):
                f.write(bloque)
    return destino


def tabla() -> pd.DataFrame:
    """Dataset completo, tipado y con columnas normalizadas para matching."""
    ruta = _descargar(url_csv())
    # Los ids INDEC son texto: "06721" no es 6721. Sin dtype explicito pandas
    # los convierte a float y se pierde el cero inicial.
    ids = {"provincia_id": str, "departamento_id": str}
    try:
        df = pd.read_csv(ruta, encoding="utf-8", dtype=ids)
    except UnicodeDecodeError:
        df = pd.read_csv(ruta, encoding="latin-1", dtype=ids)
    faltan = set(COLUMNAS) - set(df.columns)
    if faltan:
        raise RuntimeError(f"El CSV del MAGYP cambio de esquema, faltan: {sorted(faltan)}")
    df["anio"] = pd.to_numeric(df["anio"], errors="coerce").astype("Int64")
    for c in ["superficie_sembrada_ha", "superficie_cosechada_ha",
              "produccion_tm", "rendimiento_kgxha"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df["departamento_id"] = df["departamento_id"].astype(str).str.zfill(5)
    df["_cultivo"] = df["cultivo"].map(normalizar)
    df["_departamento"] = df["departamento"].map(normalizar)
    df["_provincia"] = df["provincia"].map(normalizar)
    return df


def serie(cultivo: str, partido: str | None = None, provincia: str | None = None,
          departamento_id: str | None = None, df: pd.DataFrame | None = None,
          min_superficie: float = 0.0) -> pd.DataFrame:
    """Serie historica de un cultivo en un partido.

    El partido se puede identificar por nombre (+provincia para desambiguar,
    porque hay homonimos entre provincias) o por departamento_id INDEC, que
    es lo unico realmente univoco.
    """
    d = tabla() if df is None else df
    d = d[d["_cultivo"] == normalizar(cultivo)]
    if departamento_id:
        d = d[d["departamento_id"] == str(departamento_id).zfill(5)]
    else:
        if partido:
            d = d[d["_departamento"] == normalizar(partido)]
        if provincia:
            d = d[d["_provincia"] == normalizar(provincia)]
    if min_superficie:
        d = d[d["superficie_sembrada_ha"].fillna(0) >= min_superficie]
    cols = ["anio", "campania", "provincia", "departamento", "departamento_id",
            "cultivo", "superficie_sembrada_ha", "superficie_cosechada_ha",
            "produccion_tm", "rendimiento_kgxha"]
    return d[cols].sort_values("anio").reset_index(drop=True)


def buscar_partido(texto: str, df: pd.DataFrame | None = None) -> pd.DataFrame:
    """Ayuda para encontrar el departamento_id de un partido por nombre parcial."""
    d = tabla() if df is None else df
    m = d[d["_departamento"].str.contains(normalizar(texto), na=False)]
    return (m[["provincia", "departamento", "departamento_id"]]
            .drop_duplicates().sort_values(["provincia", "departamento"])
            .reset_index(drop=True))
