"""Snapshot analitico: la frontera entre el ETL y la web.

El pipeline original bajaba MAGYP, NOAA y Open-Meteo en la misma corrida que
hacia el calculo. Eso funciona en una CLI y no funciona en una web: el CSV
del MAGYP pesa 15 MB, Open-Meteo tiene cuota horaria y un 429 duerme hasta el
cambio de hora. Ningun request HTTP puede depender de eso.

La separacion es:

    ETL (batch, mensual)      snapshot en disco        web (request)
    baja y normaliza     ->   parquet inmutable   ->   lee de RAM y calcula

El snapshot es chico a proposito: 160 mil filas de rinde son ~2 MB en parquet
y entran enteras en memoria del proceso web. No hay base de datos porque no
hace falta una: el dataset completo del pais cabe en un DataFrame.

Lo que se precomputa es el DATO, no el RESULTADO. Los estadisticos por fase
dependen de `umbral`, `desde` y `metodo_tendencia`, que son justamente las
perillas que el usuario mueve en el informe: congelarlos en una tabla de
resultados obligaria a recalcular el snapshot entero por cada combinacion.
Con el dato normalizado en RAM el calculo son milisegundos.
"""

from __future__ import annotations

import datetime as dt
import json
import os

import pandas as pd

RAIZ = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    "data", "snapshot")

# Version del esquema. Si cambia el layout de los parquet hay que subirla:
# la web se niega a levantar un snapshot de version distinta en vez de
# servir columnas que no existen.
VERSION = 1

ARCHIVOS = {
    "rindes": "rindes.parquet",
    "fases": "fases.parquet",
    "oni": "oni.parquet",
    "partidos": "partidos.parquet",
    "clima": "clima.parquet",
}


def ruta(nombre: str, raiz: str | None = None) -> str:
    return os.path.join(raiz or RAIZ, ARCHIVOS[nombre])


def ruta_geojson(raiz: str | None = None) -> str:
    """Poligonos departamentales. No es parquet: lo consume el navegador."""
    return os.path.join(raiz or RAIZ, "departamentos.geojson")


def ruta_meta(raiz: str | None = None) -> str:
    return os.path.join(raiz or RAIZ, "meta.json")


def existe(raiz: str | None = None) -> bool:
    """True si hay un snapshot minimo utilizable (rinde + fases + partidos)."""
    return all(os.path.exists(ruta(n, raiz))
               for n in ("rindes", "fases", "partidos"))


def leer_meta(raiz: str | None = None) -> dict:
    try:
        with open(ruta_meta(raiz), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def escribir_meta(meta: dict, raiz: str | None = None) -> None:
    os.makedirs(raiz or RAIZ, exist_ok=True)
    with open(ruta_meta(raiz), "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)


def guardar(nombre: str, df: pd.DataFrame, raiz: str | None = None) -> str:
    os.makedirs(raiz or RAIZ, exist_ok=True)
    r = ruta(nombre, raiz)
    df.to_parquet(r, index=False, compression="zstd")
    return r


def cargar(nombre: str, raiz: str | None = None) -> pd.DataFrame | None:
    """DataFrame del snapshot, o None si ese archivo no fue construido.

    `clima` es opcional a proposito: el ETL de Open-Meteo tarda horas por la
    cuota y puede quedar a medias. Sin clima la web sirve igual el cruce
    ENSO x rinde, que es el nucleo del informe; solo se apagan las columnas
    climaticas y el ranking de sensibilidad.
    """
    r = ruta(nombre, raiz)
    if not os.path.exists(r):
        return None
    return pd.read_parquet(r)


def sello(fuentes: dict, filas: dict, raiz: str | None = None) -> dict:
    """Metadatos de trazabilidad del snapshot.

    Un informe de riesgo sin fecha de corte del dato no sirve: el MAGYP
    publica campanias con meses de atraso y el ONI se revisa hacia atras.
    La web muestra esto en el pie de cada consulta.
    """
    return {
        "version_esquema": VERSION,
        "construido": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "fuentes": fuentes,
        "filas": filas,
    }
