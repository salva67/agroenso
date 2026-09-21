"""Cache en disco para las descargas. Evita golpear las APIs en cada corrida."""

from __future__ import annotations

import hashlib
import json
import os
import time

RAIZ = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "cache")


def _ruta(clave: str, ext: str) -> str:
    os.makedirs(RAIZ, exist_ok=True)
    h = hashlib.sha1(clave.encode("utf-8")).hexdigest()[:16]
    return os.path.join(RAIZ, f"{h}.{ext}")


def vigente(ruta: str, dias: float) -> bool:
    if not os.path.exists(ruta):
        return False
    if dias <= 0:
        return True
    return (time.time() - os.path.getmtime(ruta)) < dias * 86400


def leer_json(clave: str, dias: float):
    """Devuelve el objeto cacheado o None si no existe o vencio."""
    r = _ruta(clave, "json")
    if not vigente(r, dias):
        return None
    try:
        with open(r, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def guardar_json(clave: str, obj) -> None:
    with open(_ruta(clave, "json"), "w", encoding="utf-8") as f:
        json.dump(obj, f)


def ruta_binaria(clave: str, ext: str = "bin") -> str:
    """Ruta estable para archivos grandes (CSV del MAGYP)."""
    return _ruta(clave, ext)
