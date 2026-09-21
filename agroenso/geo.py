"""Resolucion geografica contra la API Georef de datos.gob.ar (IGN).

Sirve para los dos sentidos que hacen falta:

  coordenadas del lote  ->  departamento_id INDEC  ->  serie de rindes MAGYP
  nombre del partido    ->  centroide             ->  celda ERA5

El id que devuelve Georef es el mismo codigo INDEC que usa el MAGYP en
`departamento_id`, asi que las dos fuentes empalman sin tabla de traduccion.
"""

from __future__ import annotations


# `requests` se importa DENTRO de las funciones que salen a la red, no aca
# arriba. La imagen de la web no lo instala a proposito, y la cadena de
# imports de `web.api` pasa por este modulo: con el import a nivel de modulo,
# el proceso web no arrancaba. Asi la regla "la web no toca la red" deja de
# ser una promesa del README y pasa a estar sostenida por la estructura: si
# alguien mete una descarga en el camino de lectura, revienta al llamarla con
# un ImportError que dice exactamente que paso.
from . import cache

BASE = "https://apis.datos.gob.ar/georef/api"
TIMEOUT = 60
DIAS_CACHE = 180  # los limites departamentales no se mueven


def _get(ruta: str, params: dict) -> dict:
    import requests

    clave = "georef|" + ruta + "|" + repr(sorted(params.items()))
    d = cache.leer_json(clave, DIAS_CACHE)
    if d is None:
        r = requests.get(BASE + ruta, params=params, timeout=TIMEOUT)
        r.raise_for_status()
        d = r.json()
        cache.guardar_json(clave, d)
    return d


def departamento_de(lat: float, lon: float) -> dict:
    """Partido/departamento que contiene un punto.

    Devuelve {departamento_id, departamento, provincia_id, provincia} o {}
    si el punto cae fuera del territorio (mar, pais limitrofe).
    """
    u = _get("/ubicacion", {"lat": lat, "lon": lon}).get("ubicacion", {})
    dep = u.get("departamento") or {}
    prov = u.get("provincia") or {}
    if not dep.get("id"):
        return {}
    return {
        "departamento_id": dep["id"],
        "departamento": dep.get("nombre"),
        "provincia_id": prov.get("id"),
        "provincia": prov.get("nombre"),
    }


def centroide(departamento_id: str | None = None, nombre: str | None = None,
              provincia: str | None = None) -> dict:
    """Centroide de un partido, por id INDEC o por nombre.

    OJO: el centroide es un punto de referencia del partido, no del lote.
    En partidos grandes puede quedar a 40-50 km de los lotes reales y el
    gradiente de lluvia de la pampa es sensible a eso. Usarlo solo cuando la
    poliza no trae coordenadas.
    """
    params = {"campos": "id,nombre,centroide", "max": 5}
    if departamento_id:
        params["id"] = str(departamento_id).zfill(5)
    if nombre:
        params["nombre"] = nombre
    if provincia:
        params["provincia"] = provincia
    deps = _get("/departamentos", params).get("departamentos", [])
    if not deps:
        return {}
    d = deps[0]
    return {
        "departamento_id": d["id"],
        "departamento": d["nombre"],
        "lat": d["centroide"]["lat"],
        "lon": d["centroide"]["lon"],
        "ambiguo": len(deps) > 1,
    }


def todos_los_departamentos(provincia: str | None = None) -> list[dict]:
    """Los ~530 departamentos del pais con su centroide, en UN solo request.

    Pidiendolos de a uno por `centroide()` son 500+ llamadas para armar el
    snapshot. Georef acepta `max` alto y devuelve todo junto, asi que el ETL
    resuelve la geografia completa en una descarga y queda cacheada 180 dias.
    """
    params = {"campos": "id,nombre,centroide,provincia", "max": 1000,
              "orden": "id"}
    if provincia:
        params["provincia"] = provincia
    deps = _get("/departamentos", params).get("departamentos", [])
    out = []
    for d in deps:
        prov = d.get("provincia") or {}
        out.append({
            "departamento_id": str(d["id"]).zfill(5),
            "departamento": d.get("nombre"),
            "provincia_id": prov.get("id"),
            "provincia": prov.get("nombre"),
            "lat": d["centroide"]["lat"],
            "lon": d["centroide"]["lon"],
        })
    return out


# ---------------------------------------------------------------------------
# Geometrias departamentales
# ---------------------------------------------------------------------------

URL_GEOJSON = "https://infra.datos.gob.ar/georef/departamentos.geojson"

# La API REST de Georef solo devuelve centroides: `campos=geometria` da 400.
# Los poligonos viven en este archivo aparte, ya generalizado por el IGN
# (529 features, ~73 vertices cada uno, 1,2 MB). No hace falta simplificarlo.

# Codigos INDEC donde el MAGYP y el IGN no coinciden. Se corrige el del MAGYP
# para poder pegarle la geometria.
#
#   06217 -> 06218  Chascomus. Cuando en 2009 se separo Lezama, el INDEC
#                   renumero el partido; el MAGYP sigue publicando el codigo
#                   viejo. Sin esta linea Chascomus se cae del mapa con 496
#                   campanias a cuestas.
CORRECCION_ID = {"06217": "06218"}


def geometrias(ids: set[str] | None = None) -> dict:
    """FeatureCollection de departamentos, opcionalmente filtrado por id.

    Las propiedades se recortan a `id`: el nombre y la provincia ya viajan en
    la tabla de partidos del snapshot, y repetirlos en cada feature es peso
    muerto que el navegador descarga en cada visita.
    """
    import requests

    d = cache.leer_json(URL_GEOJSON, DIAS_CACHE)
    if d is None:
        r = requests.get(URL_GEOJSON, timeout=TIMEOUT)
        r.raise_for_status()
        d = r.json()
        cache.guardar_json(URL_GEOJSON, d)

    salida = []
    for f in d.get("features", []):
        gid = str(f["properties"]["id"]).zfill(5)
        if ids is not None and gid not in ids:
            continue
        salida.append({"type": "Feature", "properties": {"id": gid},
                       "geometry": _redondear_geom(f["geometry"])})
    return {"type": "FeatureCollection", "features": salida}


def _redondear_geom(g: dict, decimales: int = 4) -> dict:
    """Recorta las coordenadas a ~11 m.

    El archivo del IGN trae 8-9 decimales, o sea precision milimetrica, para
    un limite departamental dibujado a escala provincial. Recortar a 4 baja el
    archivo a un tercio sin que se note en pantalla.
    """
    def rec(o):
        if isinstance(o[0], (int, float)):
            return [round(o[0], decimales), round(o[1], decimales)]
        return [rec(x) for x in o]
    return {"type": g["type"], "coordinates": rec(g["coordinates"])}
