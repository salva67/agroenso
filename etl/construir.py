"""ETL del snapshot: baja las fuentes, normaliza y escribe los parquet.

Corre fuera del request. En produccion va como job mensual (el MAGYP publica
una vez por mes, el ONI tambien) y su salida es inmutable: la web nunca
escribe en `data/snapshot/`.

    python -m etl.construir
    python -m etl.construir --desde 1970 --salida /datos/snapshot

Si una fuente falla, el snapshot viejo queda intacto: se arma completo en un
directorio temporal y recien al final se promueve. Un ETL que deja la web
servida a medias es peor que un ETL que no corrio.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import shutil
import tempfile

import pandas as pd

from agroenso import cultivos, enso, geo, rindes, snapshot

# Columnas que sobreviven al snapshot. El CSV del MAGYP trae mas, pero lo que
# no se usa en el informe solo agrega peso al parquet y a la RAM del proceso.
COLS_RINDE = ["departamento_id", "departamento", "provincia", "provincia_id",
              "cultivo", "_cultivo", "anio", "campania",
              "superficie_sembrada_ha", "superficie_cosechada_ha",
              "produccion_tm", "rendimiento_kgxha"]


def _log(*a):
    print(*a, flush=True)


# ---------------------------------------------------------------------------

def construir_rindes(desde: int) -> tuple[pd.DataFrame, str]:
    """Serie MAGYP normalizada y tipada para el snapshot."""
    url = rindes.url_csv()
    d = rindes.tabla()
    d = d.dropna(subset=["anio", "departamento_id"])
    d["anio"] = d["anio"].astype(int)
    d = d[d["anio"] >= desde]
    # Sin rinde no hay nada que analizar. La superficie no cosechada no se
    # pierde: viaja en las columnas de superficie de las filas que si tienen
    # rinde, que es de donde sale `perdida_cosecha_pct`.
    d = d.dropna(subset=["rendimiento_kgxha"])

    # El MAGYP publica, por provincia, una fila con `departamento = "sin
    # definir"` y codigo XX000: es la produccion que no pudo asignarse a
    # ningun partido. No es un partido, no tiene geometria ni centroide, y en
    # una herramienta departamental solo ensucia el selector y el ranking.
    antes = len(d)
    d = d[(d["departamento"].astype(str).str.strip().str.lower() != "sin definir")
          & (~d["departamento_id"].astype(str).str.endswith("000"))
          & (d["departamento_id"].astype(str).str.len() == 5)
          & (d["departamento_id"].astype(str) != "00nan")]
    _log(f"    {antes - len(d):,} filas descartadas (agregados 'sin definir')")

    d["departamento_id"] = (d["departamento_id"].astype(str)
                            .replace(geo.CORRECCION_ID))
    d = d[COLS_RINDE].copy()
    for c in ["departamento_id", "provincia_id", "cultivo", "_cultivo",
              "departamento", "provincia", "campania"]:
        d[c] = d[c].astype("category")
    d["anio"] = d["anio"].astype("int16")
    for c in ["superficie_sembrada_ha", "superficie_cosechada_ha",
              "produccion_tm", "rendimiento_kgxha"]:
        d[c] = d[c].astype("float32")
    d = d.sort_values(["departamento_id", "_cultivo", "anio"]).reset_index(drop=True)
    return d, url


def construir_fases(anios: range) -> pd.DataFrame:
    """Fases ENSO por campania, para los dos ciclos, de una vez.

    La clasificacion depende SOLO del cultivo (via su ventana de trimestres),
    no del partido: son dos tablas de ~55 filas que antes se recalculaban una
    vez por cada consulta de cada partido.
    """
    o = enso.serie_oni()
    partes = []
    for ciclo, trimestres in (("verano", cultivos.ENSO_VERANO),
                              ("invierno", cultivos.ENSO_INVIERNO)):
        f = enso.fases_por_campania(anios, trimestres=trimestres, df_oni=o)
        f.insert(0, "ciclo", ciclo)
        # Cuantos trimestres de la ventana publico el CPC. Con la ventana
        # incompleta la campania en curso no se puede clasificar, y la web
        # tiene que decirlo en vez de mostrar una fase a medio calcular.
        cols = [f"oni_{t}" for t in trimestres]
        f["trimestres_publicados"] = f[cols].notna().sum(axis=1).astype(int)
        f["trimestres_ventana"] = len(trimestres)
        partes.append(f)
    return pd.concat(partes, ignore_index=True)


def construir_partidos(d_rindes: pd.DataFrame) -> pd.DataFrame:
    """Padron de partidos con centroide, limitado a los que tienen rinde."""
    geo_df = pd.DataFrame(geo.todos_los_departamentos())
    con_datos = (d_rindes.groupby("departamento_id", observed=True)
                 .agg(cultivos_n=("_cultivo", "nunique"),
                      campanias_n=("anio", "nunique"),
                      anio_min=("anio", "min"), anio_max=("anio", "max"))
                 .reset_index())
    con_datos["departamento_id"] = con_datos["departamento_id"].astype(str)
    # Los nombres del MAGYP y los de Georef no siempre coinciden ("Coronel de
    # Marina L. Rosales" vs "Coronel Rosales"); el id INDEC si, y es la unica
    # clave confiable entre las dos fuentes.
    nombres = (d_rindes[["departamento_id", "departamento", "provincia",
                         "provincia_id"]]
               .astype(str).drop_duplicates("departamento_id"))
    p = con_datos.merge(nombres, on="departamento_id", how="left")
    p = p.merge(geo_df[["departamento_id", "lat", "lon"]],
                on="departamento_id", how="left")
    sin_centroide = int(p["lat"].isna().sum())
    if sin_centroide:
        _log(f"    aviso: {sin_centroide} partidos del MAGYP sin centroide en "
             f"Georef (quedan sin clima; el cruce ENSO x rinde va igual)")
    return p.sort_values(["provincia", "departamento"]).reset_index(drop=True)


def construir_catalogo(d_rindes: pd.DataFrame) -> pd.DataFrame:
    """Cultivos disponibles, marcando cuales tienen ventana fenologica.

    Los 41 cultivos del MAGYP incluyen yerba, citrus y cania de azucar: se
    pueden cruzar contra la fase ENSO igual, pero sin ventana critica
    definida no hay clima ni ranking de sensibilidad. La web necesita poder
    distinguirlos para no prometer columnas que no va a llenar.
    """
    g = (d_rindes.groupby("_cultivo", observed=True)
         .agg(cultivo=("cultivo", "first"),
              partidos_n=("departamento_id", "nunique"),
              filas=("anio", "size"),
              anio_min=("anio", "min"), anio_max=("anio", "max"))
         .reset_index())
    fichas = []
    for c in g["_cultivo"]:
        try:
            f = cultivos.ficha(c)
            fichas.append({"ciclo": f["ciclo"], "ventana_fenologica": True,
                           "ficha_de": f["cultivo"]})
        except KeyError:
            # Sin ficha se asume verano para poder clasificar la campania;
            # es lo unico que se puede hacer sin inventar una fenologia.
            fichas.append({"ciclo": "verano", "ventana_fenologica": False,
                           "ficha_de": None})
    return pd.concat([g.reset_index(drop=True),
                      pd.DataFrame(fichas)], axis=1)


# ---------------------------------------------------------------------------

def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        prog="etl.construir", description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--desde", type=int, default=1970,
                   help="primera campania a incluir (default 1970)")
    p.add_argument("--salida", default=snapshot.RAIZ)
    args = p.parse_args(argv)

    hoy = dt.date.today()
    tmp = tempfile.mkdtemp(prefix="agroenso_snap_")
    try:
        _log("1/5 rindes MAGYP ...")
        d_rindes, url_magyp = construir_rindes(args.desde)
        snapshot.guardar("rindes", d_rindes, tmp)
        _log(f"    {len(d_rindes):,} filas  "
             f"{d_rindes['departamento_id'].nunique()} partidos  "
             f"{d_rindes['_cultivo'].nunique()} cultivos  "
             f"{d_rindes['anio'].min()}-{d_rindes['anio'].max()}")

        _log("2/5 ONI NOAA ...")
        o = enso.serie_oni()
        snapshot.guardar("oni", o, tmp)
        fases = construir_fases(range(args.desde, hoy.year + 1))
        snapshot.guardar("fases", fases, tmp)
        _log(f"    ONI {len(o)} trimestres, ultimo "
             f"{o.iloc[-1].trimestre} {int(o.iloc[-1].anio)} = {o.iloc[-1].oni}")

        _log("3/5 centroides Georef ...")
        partidos = construir_partidos(d_rindes)
        snapshot.guardar("partidos", partidos, tmp)
        _log(f"    {len(partidos)} partidos con datos de rinde")

        _log("4/5 geometrias departamentales ...")
        ids = set(d_rindes["departamento_id"].astype(str).unique())
        fc = geo.geometrias(ids)
        con_geo = {f["properties"]["id"] for f in fc["features"]}
        ruta_geo = os.path.join(tmp, "departamentos.geojson")
        with open(ruta_geo, "w", encoding="utf-8") as fh:
            json.dump(fc, fh, separators=(",", ":"))
        faltan = sorted(ids - con_geo)
        _log(f"    {len(fc['features'])} poligonos "
             f"({os.path.getsize(ruta_geo) / 1e6:.1f} MB)")
        if faltan:
            # Un partido sin poligono sale del mapa pero sigue en el ranking y
            # en el analisis: conviene que se sepa cuales son.
            _log(f"    aviso: {len(faltan)} partidos sin geometria: "
                 f"{', '.join(faltan[:8])}")

        _log("5/5 catalogo de cultivos ...")
        catalogo = construir_catalogo(d_rindes)
        catalogo.to_parquet(os.path.join(tmp, "catalogo.parquet"), index=False)
        _log(f"    {len(catalogo)} cultivos, "
             f"{int(catalogo['ventana_fenologica'].sum())} con ventana fenologica")

        # El clima no se reconstruye aca: su ETL tarda horas por la cuota de
        # Open-Meteo y corre aparte. Si ya habia uno, se arrastra al snapshot
        # nuevo en vez de perderse.
        viejo = snapshot.ruta("clima", args.salida)
        if os.path.exists(viejo):
            shutil.copy2(viejo, snapshot.ruta("clima", tmp))
            _log("    clima: se conserva el del snapshot anterior")

        snapshot.escribir_meta(snapshot.sello(
            fuentes={"magyp_csv": url_magyp,
                     "oni": enso.URL_ONI,
                     "georef": geo.BASE,
                     "geometrias": geo.URL_GEOJSON,
                     "campania_max": int(d_rindes["anio"].max()),
                     "oni_ultimo": f"{o.iloc[-1].trimestre} {int(o.iloc[-1].anio)}"},
            filas={"rindes": len(d_rindes), "fases": len(fases),
                   "partidos": len(partidos), "cultivos": len(catalogo),
                   "poligonos": len(fc["features"])},
        ), tmp)

        # Promocion al final: el snapshot se arma completo aparte y recien
        # entonces se mueve encima del que la web esta sirviendo.
        os.makedirs(args.salida, exist_ok=True)
        for f in os.listdir(tmp):
            shutil.move(os.path.join(tmp, f), os.path.join(args.salida, f))
        peso = sum(os.path.getsize(os.path.join(args.salida, f))
                   for f in os.listdir(args.salida)) / 1e6
        _log(f"\nsnapshot en {args.salida}  ({peso:.1f} MB)")
        return 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
