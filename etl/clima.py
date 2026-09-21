"""ETL del clima: ERA5 por centroide de partido, agregado por ventana critica.

Es el unico proceso lento del pipeline y por eso vive aparte de
`etl.construir`. Open-Meteo cobra por volumen y corta por cuota horaria: una
serie de 55 anios con 5 variables diarias pesa ~700 KB, y son ~500 partidos.
Con las pausas y los reintentos esto tarda horas y puede necesitar varias
corridas. Nada de eso puede pasar dentro de un request HTTP, y por eso el
resultado se congela en el snapshot.

    python -m etl.clima                          # todo el pais, por tandas
    python -m etl.clima --provincia "Buenos Aires"
    python -m etl.clima --max-partidos 40        # una tanda y salir

Es REANUDABLE: cada partido terminado se agrega al parquet de salida, y una
corrida nueva saltea los que ya estan. Si Open-Meteo corta por cuota diaria,
el proceso termina ordenado y lo ya bajado queda.

La clave del costo: se pide UNA serie diaria por partido que cubre todo el
periodo, y de ahi salen las ventanas de TODOS los cultivos de ese partido.
Pedir por cultivo serian 14 descargas del mismo punto.
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import sys

import pandas as pd

from agroenso import clima, cultivos, snapshot

COLS = ["departamento_id", "_cultivo", "anio",
        "vc_pp_mm", "vc_dias_lluvia", "vc_racha_seca_max", "vc_et0_mm",
        "vc_balance_mm", "vc_dias_tmax_33", "vc_dias_tmax_35", "vc_tmed",
        "cc_pp_mm", "cc_balance_mm"]


def _log(*a):
    print(*a, flush=True)


def ventanas_de(diaria: pd.DataFrame, cultivo_norm: str,
                anios: list[int]) -> list[dict]:
    """Agrega la serie diaria a las ventanas fenologicas de un cultivo."""
    try:
        f = cultivos.ficha(cultivo_norm)
    except KeyError:
        return []
    hoy = dt.date.today()
    filas = []
    for a in anios:
        ci, cf = cultivos.ventana(a, f["critica"])
        ti, tf = cultivos.ventana(a, f["ciclo_completo"])
        if cf >= hoy - dt.timedelta(days=6):
            continue                      # la ventana critica todavia no cerro
        crit = clima.agregar_ventana(diaria, ci, cf)
        # `agregar_ventana` devuelve lo que encuentre; si la serie descargada
        # no cubre la ventana entera, el acumulado de lluvia queda corto y
        # parece una sequia que no existio.
        if not crit or crit.get("dias", 0) <= (cf - ci).days:
            continue
        tot = clima.agregar_ventana(diaria, ti, tf)
        fila = {"_cultivo": cultivo_norm, "anio": a}
        fila.update({"vc_" + k: v for k, v in crit.items() if k != "dias"})
        fila["cc_pp_mm"] = tot.get("pp_mm")
        fila["cc_balance_mm"] = tot.get("balance_mm")
        filas.append(fila)
    return filas


def _unir(ya: pd.DataFrame | None, nuevas: list[dict]) -> pd.DataFrame:
    """Acumula las filas nuevas sobre lo ya bajado.

    Concatenar contra un DataFrame vacio con columnas declaradas hace que
    pandas resuelva los dtypes contra columnas todas-NA y avise por ello; con
    None de arranque el primer lote define los tipos y listo.
    """
    d = pd.DataFrame(nuevas)
    return d if ya is None or not len(ya) else pd.concat([ya, d], ignore_index=True)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        prog="etl.clima", description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--snapshot", default=snapshot.RAIZ)
    p.add_argument("--provincia", help="limitar a una provincia")
    p.add_argument("--cultivos", help="lista separada por comas; default: todos "
                                      "los que tienen ventana fenologica")
    p.add_argument("--desde", type=int, default=1980)
    p.add_argument("--max-partidos", type=int, default=0,
                   help="cortar despues de N partidos nuevos (0 = sin limite)")
    p.add_argument("--min-campanias", type=int, default=15,
                   help="saltear pares partido x cultivo con serie corta")
    args = p.parse_args(argv)

    if not snapshot.existe(args.snapshot):
        _log("No hay snapshot. Correr primero: python -m etl.construir")
        return 2

    rindes = snapshot.cargar("rindes", args.snapshot)
    partidos = snapshot.cargar("partidos", args.snapshot)
    partidos = partidos.dropna(subset=["lat", "lon"])
    if args.provincia:
        from agroenso.rindes import normalizar
        pn = normalizar(args.provincia)
        partidos = partidos[partidos["provincia"].map(normalizar) == pn]

    pedidos = (set(c.strip() for c in args.cultivos.split(",")) if args.cultivos
               else set(cultivos.CULTIVOS) | set(cultivos.ALIAS))

    rindes = rindes[rindes["anio"] >= args.desde]
    # Pares partido x cultivo que valen la pena: los que tienen serie larga.
    pares = (rindes.groupby(["departamento_id", "_cultivo"], observed=True)
             .size().reset_index(name="n"))
    pares = pares[pares["n"] >= args.min_campanias]
    pares["departamento_id"] = pares["departamento_id"].astype(str)
    pares["_cultivo"] = pares["_cultivo"].astype(str)
    pares = pares[pares["_cultivo"].isin(pedidos)]

    salida = snapshot.ruta("clima", args.snapshot)
    ya = pd.read_parquet(salida) if os.path.exists(salida) else None
    hechos = set(ya["departamento_id"].astype(str)) if ya is not None else set()

    objetivo = [d for d in partidos["departamento_id"].astype(str)
                if d in set(pares["departamento_id"]) and d not in hechos]
    _log(f"{len(objetivo)} partidos pendientes "
         f"({len(hechos)} ya en el snapshot de clima)")
    if args.max_partidos:
        objetivo = objetivo[:args.max_partidos]

    anios_de = {d: sorted(g["anio"].astype(int).unique())
                for d, g in rindes.assign(
                    departamento_id=rindes["departamento_id"].astype(str)
                ).groupby("departamento_id")}
    coord = partidos.set_index(partidos["departamento_id"].astype(str))
    culti_de = pares.groupby("departamento_id")["_cultivo"].apply(list).to_dict()

    nuevas, n = [], 0
    for dep in objetivo:
        fila_geo = coord.loc[dep]
        lat, lon = float(fila_geo["lat"]), float(fila_geo["lon"])
        anios = anios_de.get(dep, [])
        if not anios:
            continue
        # Una sola ventana global que cubre el barbecho mas temprano y la
        # cosecha mas tardia de todos los cultivos del partido.
        ini = dt.date(min(anios) - 1, 1, 1)
        fin = min(dt.date(max(anios) + 1, 12, 31),
                  dt.date.today() - dt.timedelta(days=6))
        try:
            diaria = clima.serie_diaria(lat, lon, ini, fin)
        except Exception as e:                       # noqa: BLE001
            _log(f"  {dep} {fila_geo['departamento']}: {e}")
            if "DIARIA" in str(e):
                _log("  cuota diaria agotada, corto la corrida; lo bajado queda.")
                break
            continue

        for cul in culti_de.get(dep, []):
            for f in ventanas_de(diaria, cul, anios):
                nuevas.append({"departamento_id": dep, **f})
        n += 1
        _log(f"  [{n}/{len(objetivo)}] {fila_geo['departamento']}, "
             f"{fila_geo['provincia']}  ({len(culti_de.get(dep, []))} cultivos)")

        # Guardar cada 10 partidos: si el proceso muere, no se pierde la tanda.
        if n % 10 == 0 and nuevas:
            ya = _unir(ya, nuevas)
            ya.to_parquet(salida, index=False, compression="zstd")
            nuevas = []

    if nuevas:
        ya = _unir(ya, nuevas)
    if ya is not None and len(ya):
        ya = ya.reindex(columns=COLS).drop_duplicates(
            ["departamento_id", "_cultivo", "anio"], keep="last")
        ya.to_parquet(salida, index=False, compression="zstd")
        _log(f"\nclima: {len(ya):,} filas, "
             f"{ya['departamento_id'].nunique()} partidos -> {salida}")
        _log("Reiniciar la web para que tome el clima nuevo.")
    else:
        _log("\nSin filas nuevas.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
