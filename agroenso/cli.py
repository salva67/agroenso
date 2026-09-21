"""Linea de comandos.

    python -m agroenso enso
    python -m agroenso buscar salliquelo
    python -m agroenso partido --depto-id 06721 --cultivo "soja total"
    python -m agroenso plantilla polizas.csv
    python -m agroenso polizas polizas.csv
"""

from __future__ import annotations

import argparse
import os
import sys

import pandas as pd

from . import analisis, enso, geo, graficos, polizas, reporte, rindes

SALIDAS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "salidas")
CSV_KW = dict(index=False, sep=";", decimal=",", encoding="utf-8-sig")


def _guardar(df: pd.DataFrame, nombre: str) -> str:
    os.makedirs(SALIDAS, exist_ok=True)
    ruta = os.path.join(SALIDAS, nombre)
    df.to_csv(ruta, **CSV_KW)
    print("  ->", ruta)
    return ruta


def cmd_enso(args) -> int:
    o = enso.serie_oni()
    m = enso.serie_nino34_mensual()
    ult = o.iloc[-1]
    print("ONI mas reciente:", ult.trimestre, int(ult.anio), "=", ult.oni,
          "->", enso.clasificar(ult.oni), enso.intensidad(ult.oni))
    print("Nino 3.4 mensual mas reciente:", int(m.iloc[-1].anio), int(m.iloc[-1].mes),
          "anomalia", m.iloc[-1].nino34_anom)
    print()
    print(o.tail(12)[["trimestre", "anio", "oni"]].to_string(index=False))
    print()
    f = enso.fases_por_campania(range(args.desde, ult.anio + 1))
    print(f[["campania", "oni_medio", "oni_pico", "fase", "intensidad"]]
          .tail(20).to_string(index=False))
    return 0


def cmd_buscar(args) -> int:
    print(rindes.buscar_partido(args.texto).to_string(index=False))
    return 0


def cmd_partido(args) -> int:
    dep_id, lat, lon, nombre = args.depto_id, args.lat, args.lon, args.partido

    if dep_id and (lat is None or lon is None):
        c = geo.centroide(departamento_id=dep_id)
        lat, lon, nombre = c.get("lat"), c.get("lon"), c.get("departamento")
        print(f"Centroide de {nombre} ({dep_id}): {lat:.4f}, {lon:.4f}")
        print("  (centroide del partido, no del lote: la lluvia puede diferir)")
    elif lat is not None and lon is not None and not dep_id:
        d = geo.departamento_de(lat, lon)
        dep_id, nombre = d.get("departamento_id"), d.get("departamento")
        print(f"El punto cae en {nombre} ({dep_id})")
    if not dep_id or lat is None:
        print("Falta ubicacion: pasar --depto-id o --lat/--lon", file=sys.stderr)
        return 2

    t = analisis.tabla_campanias(lat, lon, args.cultivo, departamento_id=dep_id,
                                 desde=args.desde, metodo_tendencia=args.tendencia)
    res = analisis.resumen_por_fase(t, umbral_siniestro=args.umbral)
    sens = analisis.sensibilidad(t)

    etiqueta = f"{nombre}_{args.cultivo}".replace(" ", "_").replace("/", "-")
    print()
    print(res.to_string(index=False))
    print()
    print(sens.to_string(index=False))

    _guardar(t, f"campanias_{etiqueta}.csv")
    _guardar(res, f"resumen_fase_{etiqueta}.csv")
    _guardar(sens, f"sensibilidad_{etiqueta}.csv")
    mejor = sens.iloc[0]["variable"] if len(sens) else None
    os.makedirs(SALIDAS, exist_ok=True)
    png = graficos.panel(t, f"{nombre} - {args.cultivo} ({args.desde}-hoy)",
                         os.path.join(SALIDAS, f"panel_{etiqueta}.png"), mejor)
    print("  ->", png)
    return 0


def cmd_plantilla(args) -> int:
    print("Plantilla escrita en:", polizas.plantilla(args.archivo))
    return 0


def cmd_polizas(args) -> int:
    resumen, detalle = polizas.evaluar(
        args.archivo, desde=args.desde, umbral_siniestro=args.umbral,
        metodo_tendencia=args.tendencia, hoja=args.hoja,
        campania=args.campania, estado=args.estado,
        con_clima=not args.sin_clima)
    degradadas = resumen[resumen["diagnostico"].astype(str).str.startswith("ok, sin clima")]
    if len(degradadas):
        print(f"AVISO: {degradadas['poliza'].nunique()} polizas se analizaron sin "
              f"variables climaticas (el servicio de clima fallo). Los desvios de "
              f"rinde por fase NO cambian; solo faltan las columnas de clima.")
        print()
    cartera = polizas.cartera_por_fase(resumen)
    if len(cartera):
        print("Exposicion de la cartera por fase ENSO "
              f"(umbral de siniestro {args.umbral}%):")
        print(cartera.to_string(index=False))
        print()
        _guardar(cartera, "polizas_cartera_por_fase.csv")
    _guardar(resumen, "polizas_resumen.csv")
    if detalle:
        largo = pd.concat(
            [d.assign(poliza=k) for k, d in detalle.items()], ignore_index=True)
        _guardar(largo, "polizas_detalle_campanias.csv")
    malas = resumen[~resumen["diagnostico"].astype(str).str.startswith("ok")]
    if len(malas):
        print()
        print("Polizas NO evaluadas:")
        print(malas[["poliza", "cultivo", "partido", "diagnostico"]]
              .to_string(index=False))
    return 0


def cmd_informe(args) -> int:
    resumen, _ = polizas.evaluar(
        args.archivo, desde=args.desde, umbral_siniestro=args.umbral,
        metodo_tendencia=args.tendencia, hoja=args.hoja,
        campania=args.campania, estado=args.estado,
        con_clima=not args.sin_clima)
    os.makedirs(SALIDAS, exist_ok=True)
    _guardar(resumen, "polizas_resumen.csv")
    padron = polizas.leer(args.archivo, hoja=args.hoja,
                          campania=args.campania, estado=args.estado)
    etiqueta = (args.campania or "cartera").replace("/", "-").replace(" ", "_")
    pdf = reporte.construir(
        resumen, padron, os.path.join(SALIDAS, f"informe_enso_{etiqueta}.pdf"),
        campania=(args.campania or "").replace("-", "/") or "actual",
        umbral=args.umbral)
    print("  ->", pdf)
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="agroenso", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("enso", help="estado actual del ENSO y fases historicas")
    a.add_argument("--desde", type=int, default=1980)
    a.set_defaults(func=cmd_enso)

    b = sub.add_parser("buscar", help="encontrar el departamento_id de un partido")
    b.add_argument("texto")
    b.set_defaults(func=cmd_buscar)

    c = sub.add_parser("partido", help="analisis para un partido y cultivo")
    c.add_argument("--depto-id")
    c.add_argument("--partido")
    c.add_argument("--lat", type=float)
    c.add_argument("--lon", type=float)
    c.add_argument("--cultivo", required=True)
    c.add_argument("--desde", type=int, default=1980)
    c.add_argument("--umbral", type=float, default=-15.0,
                   help="desvio %% bajo el cual la campania cuenta como siniestro")
    c.add_argument("--tendencia", choices=["movil", "lineal"], default="movil")
    c.set_defaults(func=cmd_partido)

    d = sub.add_parser("plantilla", help="generar un padron de polizas de ejemplo")
    d.add_argument("archivo")
    d.set_defaults(func=cmd_plantilla)

    e = sub.add_parser("polizas", help="evaluar un padron de polizas")
    e.add_argument("archivo")
    e.add_argument("--desde", type=int, default=1980)
    e.add_argument("--umbral", type=float, default=-15.0)
    e.add_argument("--tendencia", choices=["movil", "lineal"], default="movil")
    e.add_argument("--hoja", help="hoja del Excel (nombre o indice)")
    e.add_argument("--campania", help='filtrar por campania, p. ej. "2026-2027"')
    e.add_argument("--estado", help='filtrar por estado, p. ej. "Vigente"')
    e.add_argument("--sin-clima", action="store_true",
                   help="saltear Open-Meteo y correr solo ENSO x rinde")
    e.set_defaults(func=cmd_polizas)

    f = sub.add_parser("informe", help="evaluar el padron y generar el PDF")
    f.add_argument("archivo")
    f.add_argument("--desde", type=int, default=1980)
    f.add_argument("--umbral", type=float, default=-20.0)
    f.add_argument("--tendencia", choices=["movil", "lineal"], default="movil")
    f.add_argument("--hoja", help="hoja del Excel (nombre o indice)")
    f.add_argument("--campania", help='p. ej. "2026-2027"')
    f.add_argument("--estado", help='p. ej. "Vigente"')
    f.add_argument("--sin-clima", action="store_true",
                   help="saltear Open-Meteo y correr solo ENSO x rinde")
    f.set_defaults(func=cmd_informe)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
