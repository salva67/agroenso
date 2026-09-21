"""Clima diario ERA5 via Open-Meteo Archive API.

Sin credenciales, latencia ~5 dias, cobertura desde 1940. La grilla es de
~0.1 grados (ERA5-Land) o 0.25 (ERA5), asi que dos lotes cercanos comparten
celda: se redondean las coordenadas para reutilizar cache.

ADVERTENCIA: ERA5 es reanalisis. La precipitacion es su variable mas debil
frente a eventos convectivos aislados, que es justamente lo que rompe un
cultivo. Sirve muy bien para deficit hidrico estacional y para comparar
campanias entre si; NO reemplaza el pluviometro para peritar un siniestro.
"""

from __future__ import annotations

import datetime as dt
import time
from datetime import date

import pandas as pd

# `requests` se importa DENTRO de las funciones que salen a la red, no aca
# arriba. La imagen de la web no lo instala a proposito, y la cadena de
# imports de `web.api` pasa por este modulo: con el import a nivel de modulo,
# el proceso web no arrancaba. Asi la regla "la web no toca la red" deja de
# ser una promesa del README y pasa a estar sostenida por la estructura: si
# alguien mete una descarga en el camino de lectura, revienta al llamarla con
# un ImportError que dice exactamente que paso.
from . import cache

URL = "https://archive-api.open-meteo.com/v1/archive"
TIMEOUT = 180
TZ = "America/Argentina/Buenos_Aires"
DIAS_CACHE = 30

# Open-Meteo cobra la cuota por volumen, no por request: una serie de 45
# anios con 5 variables pesa mucho. Con una cartera de varias decenas de
# lotes se llega al 429 en menos de un minuto, asi que hay que espaciar los
# pedidos y reintentar con backoff.
PAUSA_ENTRE_PEDIDOS = 2.0
REINTENTOS = 6
ESPERA_BASE = 20.0
# Open-Meteo tiene cuota por minuto, por hora y por dia. Cuando la que se
# agota es la HORARIA no sirve el backoff exponencial corto: hay que esperar
# al cambio de hora. Se detecta por el texto de la respuesta.
MARGEN_RESET = 45.0
_ultimo_pedido = 0.0


def _segundos_hasta_proxima_hora() -> float:
    ahora = dt.datetime.now()
    proxima = (ahora + dt.timedelta(hours=1)).replace(
        minute=0, second=0, microsecond=0)
    return (proxima - ahora).total_seconds() + MARGEN_RESET

DIARIAS = [
    "precipitation_sum",
    "temperature_2m_max",
    "temperature_2m_min",
    "temperature_2m_mean",
    "et0_fao_evapotranspiration",
]

RENOMBRE = {
    "time": "fecha",
    "precipitation_sum": "pp_mm",
    "temperature_2m_max": "tmax",
    "temperature_2m_min": "tmin",
    "temperature_2m_mean": "tmed",
    "et0_fao_evapotranspiration": "et0",
}


def _redondear(x: float) -> float:
    return round(float(x), 2)


def _pedir(params: dict) -> dict:
    """GET a Open-Meteo con espaciado y reintento exponencial ante 429/5xx."""
    import requests

    global _ultimo_pedido
    ultimo_error = None
    for intento in range(REINTENTOS):
        falta = PAUSA_ENTRE_PEDIDOS - (time.monotonic() - _ultimo_pedido)
        if falta > 0:
            time.sleep(falta)
        r = requests.get(URL, params=params, timeout=TIMEOUT)
        _ultimo_pedido = time.monotonic()
        if r.status_code == 200:
            return r.json()
        if r.status_code == 429 or r.status_code >= 500:
            cuerpo = r.text[:160]
            ultimo_error = f"{r.status_code} {cuerpo}"
            if "hourly" in cuerpo.lower():
                espera = _segundos_hasta_proxima_hora()
                motivo = "cuota horaria agotada, espero al cambio de hora"
            elif "daily" in cuerpo.lower():
                raise RuntimeError(
                    "Open-Meteo: cuota DIARIA agotada. El cache conserva lo "
                    "ya bajado; reintentar maniana. " + cuerpo)
            else:
                espera = float(r.headers.get("Retry-After")
                               or ESPERA_BASE * (2 ** intento))
                motivo = "backoff"
            print(f"  Open-Meteo {r.status_code}: {motivo}, reintento en "
                  f"{espera / 60:.1f} min ({intento + 1}/{REINTENTOS})",
                  flush=True)
            time.sleep(espera)
            continue
        r.raise_for_status()
    raise RuntimeError("Open-Meteo no respondio tras "
                       f"{REINTENTOS} intentos: {ultimo_error}")


def serie_diaria(lat: float, lon: float, ini: date, fin: date) -> pd.DataFrame:
    """Serie diaria para un punto. Columnas: fecha, pp_mm, tmax, tmin, tmed, et0."""
    lat, lon = _redondear(lat), _redondear(lon)
    clave = f"om|{lat}|{lon}|{ini.isoformat()}|{fin.isoformat()}"
    d = cache.leer_json(clave, DIAS_CACHE)
    if d is None:
        d = _pedir({
            "latitude": lat,
            "longitude": lon,
            "start_date": ini.isoformat(),
            "end_date": fin.isoformat(),
            "daily": ",".join(DIARIAS),
            "timezone": TZ,
        })["daily"]
        cache.guardar_json(clave, d)
    df = pd.DataFrame(d).rename(columns=RENOMBRE)
    df["fecha"] = pd.to_datetime(df["fecha"])
    return df


def agregar_ventana(diaria: pd.DataFrame, ini: date, fin: date) -> dict:
    """Resumen de una ventana fenologica. Devuelve mm, dias de lluvia,
    balance hidrico simple (pp - et0), dias con tmax >= 33 y grados de estres."""
    m = diaria[(diaria.fecha >= pd.Timestamp(ini)) & (diaria.fecha <= pd.Timestamp(fin))]
    if m.empty:
        return {}
    pp = m["pp_mm"]
    et0 = m["et0"]
    tmax = m["tmax"]
    return {
        "dias": int(len(m)),
        "pp_mm": round(float(pp.sum()), 1),
        "dias_lluvia": int((pp >= 1.0).sum()),
        "racha_seca_max": int(_racha_seca(pp)),
        "et0_mm": round(float(et0.sum()), 1),
        "balance_mm": round(float(pp.sum() - et0.sum()), 1),
        "dias_tmax_33": int((tmax >= 33).sum()),
        "dias_tmax_35": int((tmax >= 35).sum()),
        "tmed": round(float(m["tmed"].mean()), 2),
    }


def _racha_seca(pp: pd.Series, umbral: float = 1.0) -> int:
    """Maxima cantidad de dias consecutivos con lluvia por debajo del umbral."""
    mejor = actual = 0
    for v in pp.fillna(0.0):
        actual = actual + 1 if v < umbral else 0
        mejor = max(mejor, actual)
    return mejor
