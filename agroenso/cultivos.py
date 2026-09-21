"""Ventanas fenologicas criticas por cultivo para la pampa humeda.

Cada cultivo define:
  ciclo    'verano' (se siembra en el anio Y y se cosecha en Y+1) o
           'invierno' (siembra y cosecha caen en el mismo anio Y).
  critica  ventana del periodo critico de definicion de rendimiento, como
           (mes_ini, dia_ini, offset_ini, mes_fin, dia_fin, offset_fin),
           donde offset son anios a sumar al anio de siembra de la campania.
  ciclo_completo  ventana de barbecho + ciclo, para la lluvia acumulada total.
  enso     trimestres ONI que se promedian para clasificar la campania.

Las fechas son las habituales del oeste bonaerense / sur de Cordoba. Para
otras zonas conviene ajustarlas: son el parametro mas sensible de todo el
analisis. Se pueden pisar desde un YAML con `cargar_ajustes()`.
"""

from __future__ import annotations

import datetime as dt

ENSO_VERANO = ("SON", "OND", "NDJ", "DJF")
ENSO_INVIERNO = ("JJA", "JAS", "ASO", "SON")

CULTIVOS = {
    "maiz": {
        "ciclo": "verano",
        "critica": (12, 10, 0, 1, 20, 1),      # floracion / R1 del maiz temprano
        "ciclo_completo": (9, 1, 0, 4, 30, 1),
        "enso": ENSO_VERANO,
    },
    "soja 1ra": {
        "ciclo": "verano",
        "critica": (1, 10, 1, 2, 28, 1),        # R3-R6, llenado de grano
        "ciclo_completo": (10, 1, 0, 4, 30, 1),
        "enso": ENSO_VERANO,
    },
    "soja 2da": {
        "ciclo": "verano",
        "critica": (2, 1, 1, 3, 20, 1),
        "ciclo_completo": (11, 15, 0, 5, 15, 1),
        "enso": ENSO_VERANO,
    },
    "soja total": {
        "ciclo": "verano",
        "critica": (1, 10, 1, 2, 28, 1),
        "ciclo_completo": (10, 1, 0, 4, 30, 1),
        "enso": ENSO_VERANO,
    },
    "girasol": {
        "ciclo": "verano",
        "critica": (12, 20, 0, 1, 31, 1),       # floracion
        "ciclo_completo": (9, 15, 0, 3, 15, 1),
        "enso": ENSO_VERANO,
    },
    "sorgo": {
        "ciclo": "verano",
        "critica": (1, 10, 1, 2, 20, 1),
        "ciclo_completo": (10, 1, 0, 4, 30, 1),
        "enso": ENSO_VERANO,
    },
    "mani": {
        "ciclo": "verano",
        "critica": (1, 1, 1, 2, 28, 1),
        "ciclo_completo": (10, 15, 0, 4, 15, 1),
        "enso": ENSO_VERANO,
    },
    "trigo total": {
        "ciclo": "invierno",
        "critica": (10, 1, 0, 11, 15, 0),       # espigazon y llenado
        "ciclo_completo": (4, 1, 0, 12, 15, 0),  # incluye barbecho de otonio
        "enso": ENSO_INVIERNO,
    },
    "trigo candeal": {
        "ciclo": "invierno",
        "critica": (10, 1, 0, 11, 15, 0),
        "ciclo_completo": (4, 1, 0, 12, 15, 0),
        "enso": ENSO_INVIERNO,
    },
    "cebada cervecera": {
        "ciclo": "invierno",
        "critica": (9, 20, 0, 11, 5, 0),
        "ciclo_completo": (4, 1, 0, 12, 10, 0),
        "enso": ENSO_INVIERNO,
    },
    "cebada total": {
        "ciclo": "invierno",
        "critica": (9, 20, 0, 11, 5, 0),
        "ciclo_completo": (4, 1, 0, 12, 10, 0),
        "enso": ENSO_INVIERNO,
    },
    "colza": {
        "ciclo": "invierno",
        "critica": (9, 1, 0, 10, 20, 0),
        "ciclo_completo": (3, 15, 0, 12, 1, 0),
        "enso": ENSO_INVIERNO,
    },
    "avena": {
        "ciclo": "invierno",
        "critica": (9, 15, 0, 11, 5, 0),
        "ciclo_completo": (4, 1, 0, 12, 10, 0),
        "enso": ENSO_INVIERNO,
    },
    "centeno": {
        "ciclo": "invierno",
        "critica": (9, 15, 0, 11, 5, 0),
        "ciclo_completo": (4, 1, 0, 12, 10, 0),
        "enso": ENSO_INVIERNO,
    },
}

# Cultivos sin ventana propia: se les asigna la del cultivo de referencia.
ALIAS = {
    "trigo pan": "trigo total",
    "cebada forrajera": "cebada total",
    "alpiste": "avena",
    "lino": "avena",
    "arveja": "colza",
    "mijo": "sorgo",
}


def ficha(cultivo: str) -> dict:
    from .rindes import normalizar
    c = normalizar(cultivo)
    c = ALIAS.get(c, c)
    if c not in CULTIVOS:
        raise KeyError(
            f"No hay ventana fenologica definida para '{cultivo}'. "
            f"Disponibles: {sorted(CULTIVOS)}"
        )
    return {"cultivo": c, **CULTIVOS[c]}


def ventana(anio_siembra: int, spec: tuple) -> tuple[dt.date, dt.date]:
    mi, di, oi, mf, df_, of = spec
    return (dt.date(anio_siembra + oi, mi, di), dt.date(anio_siembra + of, mf, df_))


def cargar_ajustes(ruta: str) -> None:
    """Pisa las ventanas desde un YAML, para calibrar por zona sin tocar codigo.

    Formato:
        maiz:
          critica: [12, 1, 0, 1, 10, 1]
    """
    import yaml
    with open(ruta, encoding="utf-8") as f:
        d = yaml.safe_load(f) or {}
    for cultivo, campos in d.items():
        base = CULTIVOS.setdefault(cultivo, {"ciclo": "verano", "enso": ENSO_VERANO})
        for k, v in campos.items():
            base[k] = tuple(v) if isinstance(v, list) else v
