"""Motor de lectura del snapshot. Es lo unico que corre dentro de un request.

Reglas de esta capa, que son las que hacen que la web sea servible:

  - NO toca la red. Ni MAGYP, ni NOAA, ni Open-Meteo, ni Georef. Todo lo que
    necesita ya esta en el snapshot que dejo el ETL.
  - NO escribe en disco.
  - Se carga una vez al arranque del proceso y queda caliente. El dataset
    completo del pais son ~1,2 MB en parquet y ~40 MB en RAM con los indices:
    cabe de sobra en el contenedor mas chico de cualquier PaaS.

El indice (departamento_id, cultivo) -> posiciones es lo que convierte cada
consulta en un `take` sobre un array en vez de un escaneo de 156 mil filas.
"""

from __future__ import annotations

import datetime as dt
import os

import numpy as np
import pandas as pd

from . import analisis, cultivos, enso, geo, snapshot

# Columnas de clima que el motor adjunta si el snapshot las tiene.
COLS_CLIMA = ["vc_pp_mm", "vc_balance_mm", "vc_racha_seca_max",
              "vc_dias_tmax_33", "vc_dias_tmax_35", "vc_tmed",
              "vc_dias_lluvia", "vc_et0_mm", "cc_pp_mm", "cc_balance_mm"]

FASES = ["Nino", "Neutro", "Nina"]

# Piso de campanias para que el detrend signifique algo. Por debajo de esto
# la ventana movil de 11 anios se apoya en `min_periods` y termina
# interpolando el propio dato: los desvios colapsan a cero y el informe
# miente por construccion, sin fallar.
MIN_CAMPANIAS = 15


class SnapshotAusente(RuntimeError):
    """No hay snapshot construido. La web no puede arrancar sin el."""


class SinDatos(LookupError):
    """La combinacion partido x cultivo no existe o es demasiado corta."""


def caja(v: np.ndarray) -> dict:
    """Estadisticos de un boxplot de Tukey.

    Bigotes al dato mas extremo dentro de 1,5 veces el rango intercuartil, que
    es la convencion; los puntos fuera de ahi NO se devuelven uno por uno
    porque son miles y el scatter de al lado ya muestra la nube completa.
    """
    v = np.asarray(v, dtype=float)
    v = v[np.isfinite(v)]
    if len(v) < 5:
        return {}
    q1, med, q3 = (float(x) for x in np.percentile(v, [25, 50, 75]))
    iqr = q3 - q1
    dentro = v[(v >= q1 - 1.5 * iqr) & (v <= q3 + 1.5 * iqr)]
    return {
        "n": int(len(v)),
        "q1": round(q1, 1), "mediana": round(med, 1), "q3": round(q3, 1),
        "bigote_inf": round(float(dentro.min()), 1),
        "bigote_sup": round(float(dentro.max()), 1),
        "atipicos": int(len(v) - len(dentro)),
        "media": round(float(v.mean()), 1),
    }


class Motor:
    """Snapshot cargado en memoria con sus indices.

    Instanciar esto es caro (lee los parquet); usarlo es barato. En la web hay
    exactamente una instancia por proceso, creada en el arranque.
    """

    def __init__(self, raiz: str | None = None):
        self.raiz = raiz or snapshot.RAIZ
        if not snapshot.existe(self.raiz):
            raise SnapshotAusente(
                f"No hay snapshot en {self.raiz}. Construirlo con: "
                f"python -m etl.construir")
        self.meta = snapshot.leer_meta(self.raiz)
        v = self.meta.get("version_esquema")
        if v is not None and v != snapshot.VERSION:
            raise SnapshotAusente(
                f"El snapshot es version {v} y este codigo espera "
                f"{snapshot.VERSION}. Reconstruirlo con: python -m etl.construir")

        self.rindes = snapshot.cargar("rindes", self.raiz)
        self.partidos = snapshot.cargar("partidos", self.raiz)
        self.oni = snapshot.cargar("oni", self.raiz)
        fases = snapshot.cargar("fases", self.raiz)
        cat = os.path.join(self.raiz, "catalogo.parquet")
        self.catalogo = pd.read_parquet(cat) if os.path.exists(cat) else None

        # Una tabla de fases por ciclo, indexada por anio de siembra. Antes
        # esto se recalculaba en cada consulta de cada partido; son dos
        # tablas de 57 filas que no dependen del partido en absoluto.
        self.fases = {c: g.drop(columns=["ciclo"]).set_index("anio")
                      for c, g in fases.groupby("ciclo")}

        # departamento_id x cultivo -> posiciones en `rindes`.
        self._idx = (self.rindes
                     .groupby(["departamento_id", "_cultivo"], observed=True)
                     .indices)
        self._idx = {(str(k[0]), str(k[1])): v for k, v in self._idx.items()}

        self.clima = snapshot.cargar("clima", self.raiz)
        self._idx_clima = {}
        if self.clima is not None and len(self.clima):
            self._idx_clima = {
                (str(k[0]), str(k[1])): v for k, v in
                self.clima.groupby(["departamento_id", "_cultivo"],
                                   observed=True).indices.items()}

        self._cultivos = frozenset(self.rindes["_cultivo"].astype(str).unique())
        self._ciclo_de = {}
        if self.catalogo is not None:
            self._ciclo_de = dict(zip(self.catalogo["_cultivo"].astype(str),
                                      self.catalogo["ciclo"].astype(str)))

    # -- catalogos ---------------------------------------------------------

    @property
    def tiene_clima(self) -> bool:
        return bool(self._idx_clima)

    def ciclo(self, cultivo_norm: str) -> str:
        """'verano' o 'invierno'. Define que trimestres ONI clasifican la campania."""
        if cultivo_norm in self._ciclo_de:
            return self._ciclo_de[cultivo_norm]
        try:
            return cultivos.ficha(cultivo_norm)["ciclo"]
        except KeyError:
            return "verano"

    def normalizar_cultivo(self, cultivo: str) -> str:
        """Nombre de cultivo tal como lo escribe el MAGYP, sin acentos.

        El ALIAS de `cultivos` NO se aplica aca: mapea cultivos sin ficha
        fenologica propia a la ventana de otro ("trigo pan" usa la de "trigo
        total"), que es una decision agronomica. Para BUSCAR la serie el
        nombre tiene que existir en el dataset: si no esta, no hay dato y
        conviene decirlo en vez de devolver la serie de otro cultivo.
        """
        from .rindes import normalizar
        cn = normalizar(cultivo)
        if cn not in self._cultivos:
            raise SinDatos(
                f"El MAGYP no publica un cultivo llamado {cultivo!r}. "
                f"Consultar /api/cultivos para la lista exacta.")
        return cn

    def normalizar_partido(self, departamento_id: str) -> str:
        """Id INDEC tal como quedo en el snapshot.

        El ETL corrige los codigos donde el MAGYP y el IGN no coinciden
        (`geo.CORRECCION_ID`), pero la CLI y el CSV crudo del MAGYP siguen
        publicando el viejo. Aceptar los dos evita que alguien saque el id con
        `python -m agroenso buscar` y se coma un 404 en la web.
        """
        d = str(departamento_id).strip().zfill(5)
        return geo.CORRECCION_ID.get(d, d)

    def pares_disponibles(self, minimo: int = 10) -> pd.DataFrame:
        """Combinaciones partido x cultivo con suficientes campanias.

        `minimo` no es un capricho: con menos de ~10 campanias la mediana por
        fase se apoya en 2 o 3 casos y el bootstrap devuelve un intervalo que
        ocupa toda la escala. Es preferible no ofrecer la combinacion.
        """
        g = (self.rindes.groupby(["departamento_id", "_cultivo"], observed=True)
             .agg(campanias=("anio", "size"), anio_min=("anio", "min"),
                  anio_max=("anio", "max"), cultivo=("cultivo", "first"),
                  departamento=("departamento", "first"),
                  provincia=("provincia", "first"))
             .reset_index())
        return g[g["campanias"] >= minimo].reset_index(drop=True)

    def cultivos_de(self, departamento_id: str, minimo: int = 10) -> pd.DataFrame:
        """Cultivos con serie usable en un partido, del mas largo al mas corto."""
        d = self.rindes[self.rindes["departamento_id"]
                        == self.normalizar_partido(departamento_id)]
        g = (d.groupby("_cultivo", observed=True)
             .agg(cultivo=("cultivo", "first"), campanias=("anio", "size"),
                  anio_min=("anio", "min"), anio_max=("anio", "max"),
                  superficie_med_ha=("superficie_sembrada_ha", "median"))
             .reset_index())
        g = g[g["campanias"] >= minimo]
        g["ciclo"] = g["_cultivo"].map(self.ciclo)
        g["ventana_fenologica"] = g["_cultivo"].map(
            lambda c: c in cultivos.CULTIVOS or c in cultivos.ALIAS)
        return g.sort_values("campanias", ascending=False).reset_index(drop=True)

    def buscar_partido(self, texto: str, limite: int = 25) -> pd.DataFrame:
        """Busqueda por nombre de partido o de provincia, sin acentos."""
        from .rindes import normalizar
        q = normalizar(texto)
        p = self.partidos
        m = p[p["departamento"].map(normalizar).str.contains(q, na=False)
              | p["provincia"].map(normalizar).str.contains(q, na=False)]
        return m.head(limite).reset_index(drop=True)

    # -- nucleo ------------------------------------------------------------

    def _serie(self, departamento_id: str, cultivo_norm: str) -> pd.DataFrame:
        pos = self._idx.get((self.normalizar_partido(departamento_id), cultivo_norm))
        if pos is None or len(pos) == 0:
            raise SinDatos(f"Sin serie de rinde para {departamento_id} / {cultivo_norm}")
        return self.rindes.take(pos)

    def tabla_campanias(self, departamento_id: str, cultivo: str,
                        desde: int = 1980, hasta: int | None = None,
                        metodo_tendencia: str = "movil") -> pd.DataFrame:
        """Una fila por campania: rinde, tendencia tecnologica, desvio, fase ENSO
        y — si el snapshot lo trae — el clima de la ventana critica.

        Es `analisis.tabla_campanias` sin el I/O: misma metodologia, mismo
        detrend, pero leyendo el snapshot en vez de bajar el CSV del MAGYP y
        pegarle a Open-Meteo.
        """
        cn = self.normalizar_cultivo(cultivo)
        hasta = hasta or dt.date.today().year

        r = self._serie(departamento_id, cn)
        r = r[(r["anio"] >= desde) & (r["anio"] <= hasta)].copy()
        if len(r) < MIN_CAMPANIAS:
            raise SinDatos(
                f"Solo {len(r)} campanias entre {desde} y {hasta}. Hacen falta "
                f"al menos {MIN_CAMPANIAS}: con una serie mas corta la media "
                f"movil del detrend sigue al dato en vez de a la tecnologia, "
                f"absorbe el efecto climatico y los desvios dan todos cerca de "
                f"cero. Ampliar el periodo.")
        r["anio"] = r["anio"].astype(int)

        f = self.fases[self.ciclo(cn)]
        for col in ["oni_medio", "oni_pico", "fase", "intensidad",
                    "trimestres_publicados", "trimestres_ventana"]:
            if col in f.columns:
                r[col] = r["anio"].map(f[col])

        pos = self._idx_clima.get((str(departamento_id), cn))
        if pos is not None and len(pos):
            c = self.clima.take(pos)
            cols = ["anio"] + [x for x in COLS_CLIMA if x in c.columns]
            r = r.merge(c[cols], on="anio", how="left")

        r["rinde_tendencia"] = analisis.tendencia(
            r["anio"].to_numpy(float),
            r["rendimiento_kgxha"].to_numpy(float), metodo_tendencia)
        r["desvio_kg"] = r["rendimiento_kgxha"] - r["rinde_tendencia"]
        r["desvio_pct"] = 100 * r["desvio_kg"] / r["rinde_tendencia"]
        r["perdida_cosecha_pct"] = 100 * (
            1 - r["superficie_cosechada_ha"] / r["superficie_sembrada_ha"])
        return r.reset_index(drop=True)

    def analizar(self, departamento_id: str, cultivo: str, desde: int = 1980,
                 hasta: int | None = None, umbral: float = -15.0,
                 metodo_tendencia: str = "movil") -> dict:
        """El informe completo de una combinacion partido x cultivo."""
        t = self.tabla_campanias(departamento_id, cultivo, desde, hasta,
                                 metodo_tendencia)
        res = analisis.resumen_por_fase(t, umbral_siniestro=umbral)
        sens = analisis.sensibilidad(t)
        cab = t.iloc[0]
        return {
            "partido": {
                "departamento_id": str(cab["departamento_id"]),
                "departamento": str(cab["departamento"]),
                "provincia": str(cab["provincia"]),
            },
            "cultivo": str(cab["cultivo"]),
            "ciclo": self.ciclo(str(cab["_cultivo"])),
            "parametros": {"desde": desde, "hasta": hasta or dt.date.today().year,
                           "umbral_siniestro_pct": umbral,
                           "metodo_tendencia": metodo_tendencia},
            "campanias": t,
            "resumen_fase": res,
            "sensibilidad": sens,
            "con_clima": "vc_pp_mm" in t and bool(t["vc_pp_mm"].notna().any()),
        }

    # -- ranking -----------------------------------------------------------

    def ranking(self, cultivo: str, fase: str = "Nina", desde: int = 1980,
                umbral: float = -15.0, metodo_tendencia: str = "movil",
                minimo_campanias: int = 15,
                superficie_minima_ha: float = 0.0) -> pd.DataFrame:
        """Que partidos sufren mas una fase dada, para un cultivo.

        Es la vista que convierte esto en un informe y no en una consulta
        suelta: mismo cultivo, misma fase, todos los partidos ordenados por
        cuanto se les cae el rinde.

        A diferencia de `analizar`, NO corre el bootstrap por partido: con 500
        partidos serian 6 millones de remuestreos por request. El ranking
        ordena por mediana y frecuencia de siniestro, que es lo que se mira
        para priorizar; el intervalo de confianza se ve al abrir el partido.
        """
        cn = self.normalizar_cultivo(cultivo)
        if fase not in FASES:
            raise ValueError(f"fase debe ser una de {FASES}, no {fase!r}")
        fcol = self.fases[self.ciclo(cn)]["fase"]

        filas = []
        for (dep, cul), pos in self._idx.items():
            if cul != cn:
                continue
            r = self.rindes.take(pos)
            r = r[r["anio"] >= desde]
            if len(r) < minimo_campanias:
                continue
            if superficie_minima_ha:
                med = float(r["superficie_sembrada_ha"].median())
                if not np.isfinite(med) or med < superficie_minima_ha:
                    continue
            anios = r["anio"].to_numpy(int)
            y = r["rendimiento_kgxha"].to_numpy(float)
            esp = analisis.tendencia(anios.astype(float), y, metodo_tendencia)
            # `tendencia` devuelve NaN donde no pudo estimar, y un partido con
            # un rinde 0 declarado da un esperado 0. Las dos cosas producen
            # NaN/inf aqui y se descartan dos lineas mas abajo con
            # `np.isfinite`: el warning de numpy no aporta nada y con 500
            # partidos por request inunda el log.
            with np.errstate(divide="ignore", invalid="ignore"):
                desvio = 100 * (y - esp) / esp
            fases_a = np.array([fcol.get(a, "sin dato") for a in anios])
            sel = (fases_a == fase) & np.isfinite(desvio)
            otras = (fases_a != fase) & (fases_a != "sin dato") & np.isfinite(desvio)
            if sel.sum() < 4 or otras.sum() < 4:
                continue
            d = desvio[sel]
            filas.append({
                "departamento_id": dep,
                "departamento": str(r["departamento"].iloc[0]),
                "provincia": str(r["provincia"].iloc[0]),
                "campanias_fase": int(sel.sum()),
                "campanias_total": int(np.isfinite(desvio).sum()),
                "desvio_mediano_pct": round(float(np.median(d)), 1),
                "p10_pct": round(float(np.percentile(d, 10)), 1),
                "frec_siniestro_pct": round(100 * float((d <= umbral).mean()), 1),
                "dif_vs_otras_fases": round(
                    float(np.median(d) - np.median(desvio[otras])), 1),
                "superficie_med_ha": round(
                    float(r["superficie_sembrada_ha"].median()), 0),
            })
        if not filas:
            raise SinDatos(f"Ningun partido cumple los filtros para {cultivo} / {fase}")
        return (pd.DataFrame(filas)
                .sort_values("desvio_mediano_pct")
                .reset_index(drop=True))

    def dispersion(self, cultivo: str, desde: int = 1980,
                   metodo_tendencia: str = "movil", minimo_campanias: int = 15,
                   superficie_minima_ha: float = 0.0,
                   provincia: str | None = None) -> pd.DataFrame:
        """Todas las campanias de un cultivo, en crudo: ONI contra desvio.

        Una fila por partido x campania. Es el material del scatter y del
        boxplot: a diferencia de `ranking`, que colapsa cada partido a su
        mediana, aca no se resume nada — se ve la nube entera, que es lo unico
        que muestra cuanta dispersion hay detras de esas medianas.

        El eje Y es el DESVIO, no el rinde en kg. Mezclar rindes crudos de
        partidos distintos no significa nada: un mal anio en Pergamino rinde
        mas que un buen anio en Patagones. Contra la tendencia propia de cada
        partido, si son comparables.
        """
        cn = self.normalizar_cultivo(cultivo)
        fases = self.fases[self.ciclo(cn)]
        oni_de = fases["oni_medio"].to_dict()
        fase_de = fases["fase"].to_dict()
        from .rindes import normalizar
        pn = normalizar(provincia) if provincia else None

        filas = []
        for (dep, cul), pos in self._idx.items():
            if cul != cn:
                continue
            r = self.rindes.take(pos)
            r = r[r["anio"] >= desde]
            if len(r) < minimo_campanias:
                continue
            if pn and normalizar(str(r["provincia"].iloc[0])) != pn:
                continue
            if superficie_minima_ha:
                med = float(r["superficie_sembrada_ha"].median())
                if not np.isfinite(med) or med < superficie_minima_ha:
                    continue
            anios = r["anio"].to_numpy(int)
            y = r["rendimiento_kgxha"].to_numpy(float)
            esp = analisis.tendencia(anios.astype(float), y, metodo_tendencia)
            with np.errstate(divide="ignore", invalid="ignore"):
                desvio = 100 * (y - esp) / esp
            oni = np.array([oni_de.get(int(a), np.nan) for a in anios], dtype=float)
            ok = np.isfinite(desvio) & np.isfinite(oni)
            if not ok.any():
                continue
            filas.append(pd.DataFrame({
                "departamento_id": dep,
                "anio": anios[ok],
                "oni": oni[ok],
                "desvio_pct": desvio[ok],
                "fase": [fase_de.get(int(a), "sin dato") for a in anios[ok]],
            }))
        if not filas:
            raise SinDatos(f"Sin campanias para {cultivo} con estos filtros")
        return pd.concat(filas, ignore_index=True)


    def consulta_dispersion(self, cultivo: str, **kw) -> dict:
        """Payload listo para el scatter y el boxplot, con su ajuste lineal."""
        d = self.dispersion(cultivo, **kw)
        oni = d["oni"].to_numpy(float)
        des = d["desvio_pct"].to_numpy(float)
        idx = {f: i for i, f in enumerate(FASES)}
        # La recta es un ajuste simple, no un modelo: sirve para ver el signo
        # y la magnitud de la relacion, y sobre todo para que se note lo poco
        # que explica. r2 suele quedar por debajo de 0,1.
        b, a = np.polyfit(oni, des, 1)
        r = float(np.corrcoef(oni, des)[0, 1])
        return {
            "cultivo": str(d["_nombre_cultivo"].iloc[0]) if "_nombre_cultivo" in d
                       else cultivo,
            "n": int(len(d)),
            "partidos": int(d["departamento_id"].nunique()),
            "oni": [round(float(x), 2) for x in oni],
            "desvio": [round(float(x), 1) for x in des],
            "fase": [idx.get(f, 1) for f in d["fase"]],
            "fases_orden": FASES,
            "ajuste": {"pendiente": round(float(b), 2),
                       "ordenada": round(float(a), 2),
                       "r": round(r, 3), "r2": round(r * r, 3)},
            "caja": [{"fase": f, **caja(des[d["fase"].to_numpy() == f])}
                     for f in FASES],
        }

    # -- ENSO --------------------------------------------------------------

    def enso_actual(self) -> dict:
        """Estado observado del ENSO segun el ONI del snapshot.

        Es OBSERVADO, no pronostico. La campania en curso puede tener la
        ventana de trimestres incompleta; `fases_campania` lo reporta para
        que la web no muestre una fase a medio calcular como si fuera firme.
        """
        o = self.oni
        u = o.iloc[-1]
        ult6 = o["oni"].tail(6).to_numpy(float)
        return {
            "trimestre": str(u["trimestre"]),
            "anio": int(u["anio"]),
            "oni": float(u["oni"]),
            "fase": enso.clasificar(float(u["oni"])),
            "intensidad": enso.intensidad(float(u["oni"])),
            "tendencia": ("subiendo" if ult6[-1] > ult6[0]
                          else "bajando" if ult6[-1] < ult6[0] else "estable"),
            "ultimos": o.tail(12)[["trimestre", "anio", "oni"]].to_dict("records"),
        }

    def fases_campania(self, ciclo: str = "verano", desde: int = 1980) -> pd.DataFrame:
        f = self.fases[ciclo].reset_index()
        return f[f["anio"] >= desde].reset_index(drop=True)
