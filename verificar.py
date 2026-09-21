"""Chequeo de contrato de la web. Corre en CI y contra un deploy vivo.

    python verificar.py                       # levanta la app en proceso
    python verificar.py https://mi-deploy.fly.dev

Sin framework de tests a proposito: el resto del repo no usa ninguno, y esto
tiene que poder correrse en un contenedor pelado despues de un deploy.

No valida los numeros contra valores fijos — el snapshot cambia todos los
meses y un test asi se rompe solo. Valida INVARIANTES: que las fases sumen
las campanias, que los percentiles esten ordenados, que el detrend no deje
un sesgo, que la API no devuelva NaN (que no es JSON valido) y que los
errores previstos den 404 y no 500.
"""

from __future__ import annotations

import json
import sys
import urllib.parse
import urllib.request

FALLAS: list[str] = []
CORRIDOS = 0


def check(cond, msg):
    global CORRIDOS
    CORRIDOS += 1
    if not cond:
        FALLAS.append(msg)
        print(f"  FALLA  {msg}")
    return bool(cond)


class ClienteLocal:
    """Levanta la app en proceso, sin abrir puerto."""

    def __init__(self):
        from fastapi.testclient import TestClient
        from web.api import app
        self._ctx = TestClient(app)
        self.c = self._ctx.__enter__()

    def get(self, ruta, **params):
        r = self.c.get(ruta, params=params)
        return r.status_code, (r.json() if "json" in r.headers.get("content-type", "")
                               else r.text)

    def cerrar(self):
        self._ctx.__exit__(None, None, None)


class ClienteHTTP:
    """Contra un deploy real."""

    def __init__(self, base):
        self.base = base.rstrip("/")

    def get(self, ruta, **params):
        u = self.base + ruta
        if params:
            u += "?" + urllib.parse.urlencode(params)
        try:
            with urllib.request.urlopen(u, timeout=30) as r:
                # No todo lo que sirve la API es JSON: la descarga de tablas
                # devuelve text/csv. Parsear a ciegas hacia que el chequeo de
                # la descarga volteara la corrida entera — y solo contra un
                # deploy real, porque el cliente en proceso si mira el tipo.
                cuerpo = r.read()
                if "json" not in r.headers.get("Content-Type", ""):
                    return r.status, cuerpo.decode("utf-8", "replace")
                return r.status, json.loads(cuerpo)
        except urllib.error.HTTPError as e:
            cuerpo = e.read()
            try:
                return e.code, json.loads(cuerpo)
            except ValueError:
                return e.code, cuerpo.decode("utf-8", "replace")

    def cerrar(self):
        pass


def _puntos(o):
    """Aplana coordenadas anidadas de un Polygon o MultiPolygon."""
    if isinstance(o[0], (int, float)):
        return [o]
    return [p for x in o for p in _puntos(x)]


def sin_nan(obj, ruta="") -> list[str]:
    """NaN e Infinity no son JSON valido; algunos parsers los aceptan igual.

    Si se escapa uno, el JSON sale con literales que rompen a cualquier
    consumidor estricto — y las columnas de clima estan llenas de huecos.
    """
    malos = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            malos += sin_nan(v, f"{ruta}.{k}")
    elif isinstance(obj, list):
        for i, v in enumerate(obj[:50]):
            malos += sin_nan(v, f"{ruta}[{i}]")
    elif isinstance(obj, float) and (obj != obj or obj in (float("inf"), float("-inf"))):
        malos.append(ruta)
    return malos


def main(argv):
    cli = ClienteHTTP(argv[1]) if len(argv) > 1 else ClienteLocal()
    destino = argv[1] if len(argv) > 1 else "app en proceso"
    print(f"verificando: {destino}\n")

    try:
        # -- salud y procedencia ------------------------------------------
        cod, s = cli.get("/api/salud")
        check(cod == 200, f"/api/salud devolvio {cod}")
        check(s.get("filas", 0) > 50_000, "el snapshot tiene menos filas de las esperadas")
        check(s.get("partidos", 0) > 300, "faltan partidos en el snapshot")
        print(f"  snapshot: {s.get('filas'):,} filas, {s.get('partidos')} partidos, "
              f"clima={'si' if s.get('clima') else 'no'}, del {str(s.get('construido'))[:10]}")

        cod, m = cli.get("/api/meta")
        check(cod == 200 and m.get("fuentes"), "/api/meta sin procedencia de fuentes")

        # -- ENSO ----------------------------------------------------------
        cod, e = cli.get("/api/enso")
        check(cod == 200, f"/api/enso devolvio {cod}")
        check(e.get("fase") in ("Nino", "Nina", "Neutro"), f"fase rara: {e.get('fase')}")
        check(isinstance(e.get("oni"), (int, float)), "el ONI no es numerico")

        cod, f = cli.get("/api/enso/fases", ciclo="verano", desde=1980)
        check(cod == 200 and len(f) > 30, "pocas campanias clasificadas")
        # El umbral del CPC: |ONI| >= 0,5 define fase. Si esto se rompe, el
        # cruce entero queda mal etiquetado y nada mas lo avisa.
        mal = [x for x in f if x.get("oni_medio") is not None and (
            (x["oni_medio"] >= 0.5 and x["fase"] != "Nino")
            or (x["oni_medio"] <= -0.5 and x["fase"] != "Nina")
            or (-0.5 < x["oni_medio"] < 0.5 and x["fase"] != "Neutro"))]
        check(not mal, f"{len(mal)} campanias con fase inconsistente con su ONI")

        # -- catalogos ------------------------------------------------------
        cod, provs = cli.get("/api/provincias")
        check(cod == 200 and len(provs) > 15, "faltan provincias")

        cod, parts = cli.get("/api/partidos", provincia="Buenos Aires")
        check(cod == 200 and len(parts) > 90, "faltan partidos bonaerenses")
        dep = parts[0]["departamento_id"]
        check(len(str(dep)) == 5, f"departamento_id mal formado: {dep!r}")

        # -- analisis -------------------------------------------------------
        cod, a = cli.get("/api/analisis", departamento_id="06721",
                         cultivo="maiz", desde=1980, umbral=-15)
        check(cod == 200, f"/api/analisis devolvio {cod}: {a}")
        if cod == 200:
            camp = a["campanias"]
            res = a["resumen_fase"]
            check(len(camp) > 30, "serie de campanias demasiado corta")
            check(len(res) == 3, f"deberian ser 3 fases, hay {len(res)}")

            # Las campanias clasificadas tienen que repartirse entre las tres
            # fases sin perderse ninguna ni contarse dos veces.
            con_desvio = [c for c in camp if c["desvio_pct"] is not None]
            check(sum(r["campanias"] for r in res) == len(con_desvio),
                  "las fases no suman el total de campanias con desvio")

            for r in res:
                check(r["p10_pct"] <= r["desvio_mediano_pct"] <= r["p90_pct"],
                      f"percentiles desordenados en {r['fase']}")
                check(r["ic90_inf"] <= r["ic90_sup"],
                      f"IC invertido en {r['fase']}")
                check(0 <= r["frec_siniestro_pct"] <= 100,
                      f"frecuencia fuera de rango en {r['fase']}")
                check(r["siniestros"] <= r["campanias"],
                      f"mas siniestros que campanias en {r['fase']}")

            # El detrend tiene que dejar la serie centrada: si la mediana de
            # TODOS los desvios se va lejos de cero, la tendencia esta mal
            # ajustada y cada comparacion por fase arrastra ese sesgo.
            ds = sorted(c["desvio_pct"] for c in con_desvio)
            mediana = ds[len(ds) // 2]
            check(abs(mediana) < 6,
                  f"el desvio mediano global es {mediana:.1f}%, deberia rondar 0")

            check(a.get("advertencias"), "el analisis salio sin advertencias")
            nan = sin_nan(a)
            check(not nan, f"NaN/Infinity en la respuesta: {nan[:4]}")

        # -- ranking ---------------------------------------------------------
        rk = None
        cod, rk = cli.get("/api/ranking", cultivo="maiz", fase="Nina",
                          desde=1980, minimo_campanias=20, limite=20)
        check(cod == 200, f"/api/ranking devolvio {cod}: {rk}")
        if cod == 200:
            filas = rk["ranking"]
            check(len(filas) > 5, "ranking demasiado corto")
            ordenado = all(filas[i]["desvio_mediano_pct"] <= filas[i + 1]["desvio_mediano_pct"]
                           for i in range(len(filas) - 1))
            check(ordenado, "el ranking no viene ordenado por desvio")
            check(all(f["campanias_fase"] <= f["campanias_total"] for f in filas),
                  "hay partidos con mas campanias de fase que campanias totales")
            check(not sin_nan(rk), "NaN/Infinity en el ranking")

        # -- geometrias y mapa -------------------------------------------------
        cod, g = cli.get("/api/geo/departamentos")
        check(cod == 200, f"/api/geo/departamentos devolvio {cod}")
        if cod == 200:
            feats = g.get("features", [])
            check(len(feats) > 400, f"solo {len(feats)} poligonos")
            ids_geo = {f["properties"]["id"] for f in feats}
            check(all(len(i) == 5 and i.isdigit() for i in ids_geo),
                  "hay ids de geometria mal formados")
            check(all(f["geometry"]["type"] in ("Polygon", "MultiPolygon")
                      for f in feats), "hay geometrias que no son poligonos")

            # Todo partido del padron tiene que tener su poligono. Si esto se
            # rompe, el mapa pierde partidos en silencio: siguen en el ranking
            # y en la tabla, pero el mapa queda gris y nadie se entera.
            cod2, todos = cli.get("/api/partidos", limite=2000)
            ids_padron = {p["departamento_id"] for p in todos}
            faltan = ids_padron - ids_geo
            check(not faltan,
                  f"{len(faltan)} partidos sin poligono: {sorted(faltan)[:6]}")

            # El MAGYP mete una fila por provincia con `departamento = "sin
            # definir"` y codigo XX000. No es un partido y el ETL la filtra.
            agregados = [i for i in ids_padron if i.endswith("000")]
            check(not agregados,
                  f"agregados provinciales en el padron: {agregados[:6]}")

            # Coordenadas dentro del territorio argentino continental.
            xs = [c for f in feats[:40] for c in _puntos(f["geometry"]["coordinates"])]
            check(all(-74 < lon < -52 and -56 < lat < -21 for lon, lat in xs),
                  "hay coordenadas fuera de Argentina")

        if cod == 200 and rk and "mapa" in rk:
            m = rk["mapa"]
            check(len(m) == rk["partidos_evaluados"],
                  "el mapa no trae todos los partidos evaluados")
            check(all(len(x) == 4 for x in m), "filas del mapa mal formadas")
            en_geo = sum(1 for x in m if x[0] in ids_geo)
            check(en_geo == len(m),
                  f"{len(m) - en_geo} partidos del mapa sin poligono")
            # El ranking es un recorte del mapa: los mismos numeros.
            por_id = {x[0]: x[1] for x in m}
            desaj = [r["departamento_id"] for r in rk["ranking"]
                     if abs(por_id.get(r["departamento_id"], 1e9)
                            - r["desvio_mediano_pct"]) > 0.05]
            check(not desaj, f"mapa y ranking discrepan en {desaj[:4]}")

        # -- errores previstos: 404, nunca 500 --------------------------------
        for params, que in [
            (dict(departamento_id="99999", cultivo="maiz"), "partido inexistente"),
            (dict(departamento_id="06721", cultivo="quinoa"), "cultivo inexistente"),
            (dict(departamento_id="06721", cultivo="banana"), "cultivo sin serie ahi"),
            (dict(departamento_id="06721", cultivo="maiz", desde=2020), "serie corta"),
        ]:
            cod, d = cli.get("/api/analisis", **params)
            check(cod == 404, f"{que}: devolvio {cod} en vez de 404")
            check(isinstance(d, dict) and d.get("detail"),
                  f"{que}: el 404 salio sin explicacion")

        # -- el HTML y sus estaticos --------------------------------------
        # Un JS cacheado junto a un HTML nuevo deja la pagina cargando para
        # siempre: el JS viejo busca elementos que el HTML ya no tiene y muere
        # en la inicializacion. La huella en la URL es lo que lo evita.
        cod, html = cli.get("/")
        check(cod == 200, f"la raiz devolvio {cod}")
        if cod == 200 and isinstance(html, str):
            check("/static/app.js?v=" in html,
                  "el HTML no versiona app.js: un cache viejo rompe la pagina")
            check("/static/estilo.css?v=" in html,
                  "el HTML no versiona estilo.css")
            check("g-mapa" in html, "el HTML no trae el contenedor del mapa")
            check("g-dispersion" in html,
                  "el HTML no trae el contenedor del scatter/boxplot")

        # -- dispersion --------------------------------------------------------
        cod, dsp = cli.get("/api/dispersion", cultivo="maiz", desde=1980,
                           superficie_minima_ha=3000)
        check(cod == 200, f"/api/dispersion devolvio {cod}: {dsp}")
        if cod == 200:
            n = dsp["n"]
            check(len(dsp["oni"]) == n and len(dsp["desvio"]) == n
                  and len(dsp["fase"]) == n,
                  "los arrays paralelos de la nube no tienen el mismo largo")
            check(all(0 <= f < len(dsp["fases_orden"]) for f in dsp["fase"]),
                  "hay indices de fase fuera de rango")
            check(sum(c["n"] for c in dsp["caja"] if c.get("n")) == n,
                  "las cajas no suman el total de puntos de la nube")
            for c in dsp["caja"]:
                if not c.get("n"):
                    continue
                check(c["bigote_inf"] <= c["q1"] <= c["mediana"] <= c["q3"]
                      <= c["bigote_sup"],
                      f"boxplot desordenado en {c['fase']}")
            check(not sin_nan(dsp["ajuste"]), "el ajuste trae NaN")

        # -- descarga ---------------------------------------------------------
        cod, csv = cli.get("/api/analisis.csv", departamento_id="06721",
                           cultivo="maiz", tabla="resumen")
        check(cod == 200, f"la descarga CSV devolvio {cod}")
        if cod == 200 and isinstance(csv, str):
            lineas = [x for x in csv.splitlines() if x.strip()]
            check(len(lineas) == 4, f"el CSV trae {len(lineas)} lineas, "
                                    f"esperaba encabezado + 3 fases")
            check(";" in lineas[0] and "fase" in lineas[0].lower(),
                  "el CSV no sale con separador ';' y encabezado de fases")
    finally:
        cli.cerrar()

    print(f"\n{CORRIDOS} chequeos, {len(FALLAS)} fallas")
    if FALLAS:
        print("\n".join("  - " + f for f in FALLAS))
        return 1
    print("todo bien")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
