"""Graficos del analisis. Solo matplotlib.

Nota de entorno: en esta maquina la exportacion a PNG de Plotly no funciona
(kaleido 1.x incompatible con Plotly 5.x, y kaleido 0.2.1 se cuelga). Todo
lo que tenga que salir como imagen se dibuja con matplotlib.
"""

from __future__ import annotations

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402

COLOR = {"Nino": "#c0392b", "Nina": "#2471a3", "Neutro": "#7f8c8d",
         "sin dato": "#bdc3c7"}

# Internamente las fases van sin acentos, como el resto del paquete; en los
# graficos, que son entregable, se muestran bien escritas.
ETIQUETA = {"Nino": "El Niño", "Nina": "La Niña", "Neutro": "Neutro"}


def comparacion_partidos(resumen, cultivo: str, titulo: str, ruta: str) -> str:
    """Desvio mediano por fase en cada partido de la cartera, para un cultivo.

    El punto del grafico es la CONSISTENCIA del signo entre partidos: una
    teleconexion regional real deberia empujar a todos para el mismo lado,
    aunque en cada partido por separado el intervalo cruce el cero.
    """
    d = resumen[resumen["diagnostico"].astype(str).str.startswith("ok")
                & (resumen["cultivo"] == cultivo)]
    p = (d.drop_duplicates(subset=["departamento_id", "fase"])
         .pivot_table(index="partido", columns="fase", values="desvio_mediano_pct"))
    p = p.reindex(columns=[c for c in ["Nino", "Neutro", "Nina"] if c in p])
    p = p.sort_values("Nino")

    fig, ax = plt.subplots(figsize=(9, 0.42 * len(p) + 2.2))
    y = np.arange(len(p))
    for fase in p.columns:
        ax.scatter(p[fase], y, s=70, color=COLOR.get(fase, "#95a5a6"),
                   label=ETIQUETA.get(fase, fase), edgecolor="white",
                   linewidth=0.8, zorder=3)
    for i in y:
        ax.plot([p.iloc[i].min(), p.iloc[i].max()], [i, i],
                color="#bdc3c7", lw=1.4, zorder=1)
    ax.axvline(0, color="#2c3e50", lw=1.2)
    ax.set_yticks(y)
    ax.set_yticklabels(p.index)
    ax.set_xlabel("desvio mediano del rinde respecto de la tendencia (%)")
    ax.set_title(titulo)
    ax.legend(frameon=False, ncol=3, loc="lower right")
    ax.grid(alpha=0.25, axis="x")
    fig.tight_layout()
    fig.savefig(ruta, dpi=140)
    plt.close(fig)
    return ruta


def panel(t, titulo: str, ruta: str, mejor_predictor: str | None = None) -> str:
    """Tres paneles: serie con tendencia, desvio por fase, y dispersion
    contra la variable climatica que mejor explica el desvio."""
    d = t.dropna(subset=["desvio_pct"]).copy()
    fig, ax = plt.subplots(1, 3, figsize=(16, 4.8))

    # 1. Rinde observado vs tendencia tecnologica
    ax[0].plot(t["anio"], t["rinde_tendencia"], color="#34495e", lw=1.6,
               label="tendencia tecnologica", zorder=2)
    for fase, g in d.groupby("fase"):
        ax[0].scatter(g["anio"], g["rendimiento_kgxha"], s=42,
                      color=COLOR.get(fase, "#95a5a6"),
                      label=ETIQUETA.get(fase, fase),
                      edgecolor="white", linewidth=0.6, zorder=3)
    ax[0].set_title("Rinde y tendencia")
    ax[0].set_xlabel("anio de siembra")
    ax[0].set_ylabel("kg/ha")
    ax[0].legend(fontsize=8, frameon=False)
    ax[0].grid(alpha=0.25)

    # 2. Distribucion del desvio por fase
    fases = [f for f in ["Nino", "Neutro", "Nina"] if (d["fase"] == f).any()]
    datos = [d.loc[d.fase == f, "desvio_pct"].to_numpy() for f in fases]
    bp = ax[1].boxplot(datos, tick_labels=[ETIQUETA.get(f, f) for f in fases],
                       patch_artist=True, widths=0.55)
    for caja, f in zip(bp["boxes"], fases):
        caja.set_facecolor(COLOR.get(f, "#95a5a6"))
        caja.set_alpha(0.55)
    for mediana in bp["medians"]:
        mediana.set_color("#2c3e50")
    for i, (f, v) in enumerate(zip(fases, datos), start=1):
        ax[1].scatter(np.full(len(v), i) + np.random.default_rng(3).normal(0, 0.05, len(v)),
                      v, s=18, color="#2c3e50", alpha=0.55, zorder=3)
    ax[1].axhline(0, color="#2c3e50", lw=1)
    ax[1].axhline(-15, color="#c0392b", lw=1, ls="--")
    ax[1].text(0.02, -14, "umbral siniestro -15%", color="#c0392b", fontsize=8,
               transform=ax[1].get_yaxis_transform())
    ax[1].set_title("Desvio del rinde por fase ENSO")
    ax[1].set_ylabel("% respecto de la tendencia")
    ax[1].grid(alpha=0.25, axis="y")

    # 3. Dispersion contra el mejor predictor climatico
    if mejor_predictor and mejor_predictor in d:
        s = d.dropna(subset=[mejor_predictor])
        for fase, g in s.groupby("fase"):
            ax[2].scatter(g[mejor_predictor], g["desvio_pct"], s=42,
                          color=COLOR.get(fase, "#95a5a6"),
                          label=ETIQUETA.get(fase, fase),
                          edgecolor="white", linewidth=0.6)
        if len(s) > 2:
            x = s[mejor_predictor].to_numpy(float)
            y = s["desvio_pct"].to_numpy(float)
            b, a = np.polyfit(x, y, 1)
            xs = np.linspace(x.min(), x.max(), 50)
            ax[2].plot(xs, b * xs + a, color="#34495e", lw=1.4)
            r = np.corrcoef(x, y)[0, 1]
            ax[2].set_title(f"Desvio vs {mejor_predictor}  (r2={r*r:.2f})")
        ax[2].axhline(0, color="#2c3e50", lw=1)
        ax[2].set_xlabel(mejor_predictor)
        ax[2].set_ylabel("desvio %")
        ax[2].legend(fontsize=8, frameon=False)
        ax[2].grid(alpha=0.25)
    else:
        ax[2].axis("off")

    fig.suptitle(titulo, fontsize=13)
    fig.tight_layout(rect=(0, 0, 1, 0.94))
    fig.savefig(ruta, dpi=140)
    plt.close(fig)
    return ruta
