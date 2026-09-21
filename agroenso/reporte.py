"""Informe PDF del analisis de cartera.

Se arma a partir de las salidas del pipeline (polizas_resumen.csv y el
padron), no de numeros escritos a mano: regenerar el analisis y volver a
correr esto alcanza para actualizar el informe entero.

Nota: los comentarios y nombres van sin acentos, como el resto del paquete,
pero el TEXTO DEL INFORME va acentuado. Es un entregable en castellano y las
fuentes base de reportlab (WinAnsi) resuelven bien tildes, enie y signos de
apertura.
"""

from __future__ import annotations

import datetime as dt
import os
from math import comb

import numpy as np
import pandas as pd
from reportlab.lib import colors
from reportlab.lib.enums import TA_JUSTIFY
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import cm
from reportlab.platypus import (Image, KeepTogether, Paragraph,
                                SimpleDocTemplate, Spacer, Table, TableStyle)

from . import enso, graficos

TINTA = colors.HexColor("#1f2d3d")
GRIS = colors.HexColor("#7f8c8d")
GRIS_CLARO = colors.HexColor("#eef1f4")
ROJO = colors.HexColor("#c0392b")
AZUL = colors.HexColor("#2471a3")

ORDEN_FASE = {"Nino": 0, "Neutro": 1, "Nina": 2}
ETIQUETA_FASE = {"Nino": "El Niño", "Neutro": "Neutro", "Nina": "La Niña"}

# Los cultivos vienen del vocabulario del MAGYP; para el informe se muestran
# con su nombre corriente.
ETIQUETA_CULTIVO = {
    "trigo total": "Trigo", "soja total": "Soja", "soja 1ra": "Soja de 1ª",
    "soja 2da": "Soja de 2ª", "maiz": "Maíz", "girasol": "Girasol",
    "cebada total": "Cebada", "sorgo": "Sorgo", "avena": "Avena",
    "centeno": "Centeno", "colza": "Colza", "mani": "Maní",
}


def _cult(c: str) -> str:
    return ETIQUETA_CULTIVO.get(c, str(c).capitalize())


# ---------------------------------------------------------------------------
# Estilos
# ---------------------------------------------------------------------------

def _estilos() -> dict:
    s = getSampleStyleSheet()
    return {
        "titulo": ParagraphStyle(
            "titulo", parent=s["Title"], fontName="Helvetica-Bold",
            fontSize=20, leading=24, textColor=TINTA, spaceAfter=4),
        "subtitulo": ParagraphStyle(
            "subtitulo", parent=s["Normal"], fontName="Helvetica",
            fontSize=10.5, leading=14, textColor=GRIS, spaceAfter=16),
        "h1": ParagraphStyle(
            "h1", parent=s["Heading1"], fontName="Helvetica-Bold",
            fontSize=13, leading=16, textColor=TINTA,
            spaceBefore=15, spaceAfter=7),
        "h2": ParagraphStyle(
            "h2", parent=s["Heading2"], fontName="Helvetica-Bold",
            fontSize=10.5, leading=13, textColor=TINTA,
            spaceBefore=10, spaceAfter=3),
        "cuerpo": ParagraphStyle(
            "cuerpo", parent=s["Normal"], fontName="Helvetica",
            fontSize=9.5, leading=13.5, textColor=TINTA,
            alignment=TA_JUSTIFY, spaceAfter=7),
        "nota": ParagraphStyle(
            "nota", parent=s["Normal"], fontName="Helvetica-Oblique",
            fontSize=8, leading=11, textColor=GRIS, spaceAfter=9),
        "alerta": ParagraphStyle(
            "alerta", parent=s["Normal"], fontName="Helvetica",
            fontSize=9.5, leading=13.5, textColor=TINTA,
            alignment=TA_JUSTIFY, leftIndent=9, rightIndent=9,
            spaceBefore=5, spaceAfter=5),
    }


def _tabla(datos, anchos, alinear_der=None, resaltar=None) -> Table:
    t = Table(datos, colWidths=anchos, repeatRows=1, hAlign="LEFT")
    estilo = [
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 8.2),
        ("TEXTCOLOR", (0, 0), (-1, -1), TINTA),
        ("BACKGROUND", (0, 0), (-1, 0), GRIS_CLARO),
        ("LINEBELOW", (0, 0), (-1, 0), 0.8, TINTA),
        ("LINEBELOW", (0, 1), (-1, -2), 0.25, colors.HexColor("#d5dbe1")),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("LEFTPADDING", (0, 0), (-1, -1), 5),
    ]
    for c in (alinear_der or []):
        estilo.append(("ALIGN", (c, 0), (c, -1), "RIGHT"))
    for fila, color in (resaltar or []):
        estilo.append(("TEXTCOLOR", (1, fila), (-1, fila), color))
        estilo.append(("FONTNAME", (0, fila), (-1, fila), "Helvetica-Bold"))
    t.setStyle(TableStyle(estilo))
    return t


def _caja(parrafos, color_borde, estilos) -> Table:
    inner = [[Paragraph(p, estilos["alerta"])] for p in parrafos]
    t = Table(inner, colWidths=[16.4 * cm], hAlign="LEFT")
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#fbfcfd")),
        ("LINEBEFORE", (0, 0), (0, -1), 2.5, color_borde),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("LEFTPADDING", (0, 0), (-1, -1), 9),
    ]))
    return t


def _num(x, dec=0) -> str:
    """Formato argentino: punto de miles, coma decimal."""
    if pd.isna(x):
        return "s/d"
    s = f"{float(x):,.{dec}f}"
    return s.replace(",", "@").replace(".", ",").replace("@", ".")


def _pct(x, dec=1) -> str:
    if pd.isna(x):
        return "s/d"
    return ("+" if float(x) > 0 else "") + _num(x, dec) + "%"


def _p(valor: float) -> str:
    """p-valor con coma decimal."""
    if valor < 0.0001:
        return "p &lt; 0,0001"
    dec = 2 if valor >= 0.01 else 4
    return "p = " + f"{valor:.{dec}f}".replace(".", ",")


# ---------------------------------------------------------------------------
# Estadistica de apoyo
# ---------------------------------------------------------------------------

def test_signos(pivot: pd.DataFrame, a: str, b: str) -> dict:
    """Test de signos bilateral sobre la diferencia entre dos fases.

    Mide si el signo de (fase a - fase b) se repite entre partidos mas de lo
    que explicaria el azar. OJO: los partidos NO son independientes entre si
    -comparten region y el mismo forzante ENSO-, asi que el p exagera la
    evidencia. Vale como indicio de coherencia regional, no como prueba.
    """
    if a not in pivot or b not in pivot:
        return {}
    dif = (pivot[a] - pivot[b]).dropna()
    n = len(dif)
    if n == 0:
        return {}
    k = int((dif > 0).sum())
    cola = sum(comb(n, i) for i in range(0, min(k, n - k) + 1))
    return {"n": n, "k": k, "mediana": float(dif.median()),
            "p": min(cola * 2 / 2 ** n, 1.0)}


# ---------------------------------------------------------------------------
# Informe
# ---------------------------------------------------------------------------

def construir(resumen: pd.DataFrame, padron: pd.DataFrame, ruta: str,
              campania: str = "2026/2027", umbral: float = -20.0,
              dir_graficos: str | None = None) -> str:
    E = _estilos()
    dir_graficos = dir_graficos or os.path.dirname(ruta) or "."
    ok = resumen[resumen["diagnostico"].astype(str).str.startswith("ok")].copy()
    hist = []

    # --- datos derivados -------------------------------------------------
    polizas = ok.drop_duplicates(subset=["poliza", "lat", "lon", "cultivo"])
    n_pol = len(polizas)
    ha_tot = float(polizas["superficie_ha"].fillna(0).sum())
    cap_ars = float(polizas.get("capital_ars", pd.Series(dtype=float)).fillna(0).sum())
    cap_usd = float(polizas.get("capital_usd", pd.Series(dtype=float)).fillna(0).sum())

    filas = []
    for (cult, fase), g in ok.groupby(["cultivo", "fase"]):
        w = g["superficie_ha"].fillna(0).to_numpy(float)
        tot = w.sum()
        if tot <= 0:
            w, tot = np.ones(len(g)), float(len(g))
        filas.append({
            "cultivo": cult, "fase": fase, "polizas": len(g),
            "ha": float(g["superficie_ha"].fillna(0).sum()),
            "desvio": float((g["desvio_mediano_pct"] * w).sum() / tot),
            "frec": float((g["frec_siniestro_pct"] * w).sum() / tot),
            "p10": float((g["p10_pct"] * w).sum() / tot)})
    pc = pd.DataFrame(filas)

    principal = ok.groupby("cultivo")["superficie_ha"].sum().idxmax()
    sub = ok[ok.cultivo == principal].drop_duplicates(
        subset=["departamento_id", "fase"])
    piv = sub.pivot_table(index="partido", columns="fase",
                          values="desvio_mediano_pct")
    piv_frec = sub.pivot_table(index="partido", columns="fase",
                               values="frec_siniestro_pct")
    sig_desvio = test_signos(piv, "Nina", "Nino")
    sig_frec = test_signos(piv_frec, "Nino", "Nina")

    unicas = ok.drop_duplicates(subset=["departamento_id", "cultivo", "fase"])
    significativas = unicas[(unicas.ic90_inf > 0) | (unicas.ic90_sup < 0)]

    est = enso.estado_actual()
    from . import cultivos as _c
    cob = enso.cobertura_ventana(int(str(campania)[:4]), _c.ENSO_INVIERNO)

    tri = pc[pc.cultivo == principal].set_index("fase")
    d_nino = tri.loc["Nino", "desvio"] if "Nino" in tri.index else np.nan
    d_nina = tri.loc["Nina", "desvio"] if "Nina" in tri.index else np.nan

    # --- portada ---------------------------------------------------------
    hist.append(Paragraph(
        f"Riesgo climático ENSO de la cartera {campania}", E["titulo"]))
    hist.append(Paragraph(
        f"{n_pol} pólizas vigentes &nbsp;|&nbsp; {_num(ha_tot)} ha "
        f"&nbsp;|&nbsp; ARS {_num(cap_ars)} + USD {_num(cap_usd)}<br/>"
        f"Generado el {dt.date.today().strftime('%d/%m/%Y')} &nbsp;|&nbsp; "
        f"Serie de referencia 1980-2024", E["subtitulo"]))

    # --- resumen ejecutivo ----------------------------------------------
    hist.append(Paragraph("Resumen ejecutivo", E["h1"]))
    hist.append(_caja([
        f"<b>El Niño está en desarrollo.</b> El último ONI "
        f"publicado es {est['oni_trimestre']} {est['oni_anio']} = "
        f"{_num(est['oni'], 2)}, y la anomalía mensual de Niño 3.4 de "
        f"{est['nino34_mes']}/{est['nino34_anio']} es "
        f"{_num(est['nino34_anom'], 2)}, en ascenso.",

        f"<b>La ventana ENSO de la campaña todavía no cerró:</b> "
        f"{cob['n']} de {cob['de']} trimestres publicados por el CPC. La fase "
        f"{campania} es una proyección sobre la trayectoria observada, no "
        f"un dato cerrado.",

        f"<b>Bajo El Niño, el {_cult(principal).lower()} de la cartera "
        f"rinde por debajo de su tendencia:</b> {_pct(d_nino)} mediano contra "
        f"{_pct(d_nina)} en años Niña. La brecha es de "
        f"{_num(abs(d_nino - d_nina), 1)} puntos porcentuales.",

        f"<b>La dirección es unánime entre partidos</b> "
        f"({sig_desvio['k']} de {sig_desvio['n']}), pero <b>la frecuencia de "
        f"campañas catastróficas no cambia de forma clara</b> "
        f"({sig_frec['k']} de {sig_frec['n']}, {_p(sig_frec['p'])}). El "
        f"Niño corre hacia abajo el centro de la distribución; no "
        f"engorda la cola.",
    ], AZUL, E))

    # --- composicion -----------------------------------------------------
    hist.append(Paragraph("1. Composición de la cartera", E["h1"]))
    comp = (polizas.groupby("cultivo")
            .agg(polizas=("poliza", "size"), ha=("superficie_ha", "sum"))
            .reset_index().sort_values("ha", ascending=False))
    datos = [["Cultivo", "Pólizas", "Hectáreas", "% de la superficie"]]
    for _, r in comp.iterrows():
        datos.append([_cult(r["cultivo"]), _num(r["polizas"]), _num(r["ha"]),
                      _num(100 * r["ha"] / ha_tot, 1) + "%"])
    datos.append(["Total", _num(n_pol), _num(ha_tot), "100,0%"])
    hist.append(_tabla(datos, [5.2 * cm, 2.6 * cm, 3.2 * cm, 4 * cm],
                       alinear_der=[1, 2, 3],
                       resaltar=[(len(datos) - 1, TINTA)]))
    hist.append(Spacer(1, 5))
    origen = polizas["origen_geo"].value_counts().to_dict()
    hist.append(Paragraph(
        "Ubicación resuelta: "
        + ", ".join(f"{v} por {k}" for k, v in origen.items())
        + ". Las pólizas georreferenciadas al lote permiten leer el clima "
        "en su propia celda de grilla; las resueltas por centroide de partido "
        "pueden estar a 40-50 km de los lotes reales.", E["nota"]))

    # --- resultado principal --------------------------------------------
    hist.append(Paragraph("2. Rinde esperado según la fase ENSO", E["h1"]))
    hist.append(Paragraph(
        "Todos los rindes se comparan contra su <b>tendencia "
        "tecnológica</b>, no en crudo. El rinde de cualquier partido de "
        "la pampa sube entre 1 y 2% por año por genética y manejo; "
        "sin quitar esa tendencia, comparar fases ENSO mide en qué "
        "década cayó cada fase, no el efecto del clima.", E["cuerpo"]))

    datos = [["Cultivo", "ha", "Fase", "Desvío mediano",
              "P10 (mala campaña)", f"Frec. bajo {_num(umbral)}%"]]
    resalte = []
    for cult in comp["cultivo"]:
        s = pc[pc.cultivo == cult].sort_values(
            "fase", key=lambda x: x.map(ORDEN_FASE))
        for i, (_, r) in enumerate(s.iterrows()):
            datos.append([
                _cult(cult) if i == 0 else "", _num(r["ha"]) if i == 0 else "",
                ETIQUETA_FASE.get(r["fase"], r["fase"]),
                _pct(r["desvio"]), _pct(r["p10"]), _num(r["frec"], 1) + "%"])
            if r["fase"] == "Nino" and cult == principal:
                resalte.append((len(datos) - 1, ROJO))
    hist.append(_tabla(datos, [3 * cm, 1.9 * cm, 2.3 * cm, 3.1 * cm,
                               3.3 * cm, 3 * cm],
                       alinear_der=[1, 3, 4, 5], resaltar=resalte))
    hist.append(Spacer(1, 4))
    hist.append(Paragraph(
        "Ponderado por superficie. La suma asegurada no se usa como peso "
        "porque el padrón mezcla pólizas en pesos y en dólares y "
        "no es comparable entre filas.", E["nota"]))

    hist.append(Paragraph(
        "<b>El signo se invierte entre fina y gruesa.</b> Es la "
        "teleconexión conocida y funciona como validación del "
        "método: El Niño trae primavera húmeda, buena para "
        "maíz y soja, mala para un trigo que se cosecha en diciembre "
        "entre fusarium, vuelco y brotado. La Niña, al revés. Por eso "
        "el número global de cartera promedia los dos signos y no dice "
        "nada útil: hay que leerlo por cultivo.", E["cuerpo"]))

    # --- grafico ---------------------------------------------------------
    png = os.path.join(dir_graficos, "_reporte_partidos.png")
    graficos.comparacion_partidos(
        resumen, principal,
        f"{_cult(principal)} - desvío del rinde por fase ENSO (1980-2024)",
        png)
    hist.append(KeepTogether([
        Paragraph(f"3. Consistencia entre partidos ({_cult(principal).lower()})",
                  E["h1"]),
        Image(png, width=15.4 * cm, height=15.4 * cm * 0.78, kind="proportional"),
        Spacer(1, 6),
        Paragraph(
            f"En {sig_desvio['k']} de los {sig_desvio['n']} partidos con "
            f"{_cult(principal).lower()} de la cartera, el rinde mediano en "
            f"años Niña supera al de años Niño. La mediana "
            f"de la brecha es de {_num(sig_desvio['mediana'], 1)} puntos "
            f"porcentuales.", E["cuerpo"]),
    ]))

    # --- significancia ---------------------------------------------------
    hist.append(Paragraph("4. Cuánto pesa estadísticamente", E["h1"]))
    hist.append(Paragraph(
        f"<b>Póliza por póliza, poco.</b> Solo "
        f"{len(significativas)} de {len(unicas)} combinaciones "
        f"partido-cultivo-fase tienen un intervalo de confianza del 90% que "
        f"excluye el cero. Con unas 10 campañas Niño en 45 años, "
        f"ningún partido aislado alcanza para descartar el azar.",
        E["cuerpo"]))
    if len(significativas):
        datos = [["Partido", "Cultivo", "Fase", "Campañas",
                  "Dif. vs resto", "IC 90%"]]
        for _, r in significativas.sort_values("dif_mediana_vs_resto").iterrows():
            datos.append([
                str(r["partido"]), _cult(r["cultivo"]),
                ETIQUETA_FASE.get(r["fase"], r["fase"]),
                _num(r["campanias_fase"]), _pct(r["dif_mediana_vs_resto"]),
                f"{_num(r['ic90_inf'], 1)} a {_num(r['ic90_sup'], 1)}"])
        hist.append(_tabla(datos, [4.6 * cm, 2.7 * cm, 2 * cm, 2.1 * cm,
                                   2.5 * cm, 2.7 * cm], alinear_der=[3, 4, 5]))
        hist.append(Spacer(1, 8))

    hist.append(Paragraph(
        f"<b>Lo que sí pesa es la coherencia regional.</b> Un test de "
        f"signos sobre los {sig_desvio['n']} partidos da "
        f"{_p(sig_desvio['p'])} para la diferencia de desvío mediano entre "
        f"Niña y Niño. Con la salvedad importante de que esos "
        f"{sig_desvio['n']} partidos <b>no son muestras independientes</b>: "
        f"comparten región y el mismo forzante, así que ese p exagera "
        f"la evidencia. Vale como indicio de que el gradiente regional es real "
        f"y apunta para un lado, no como {sig_desvio['n']} confirmaciones.",
        E["cuerpo"]))
    hist.append(Paragraph(
        f"<b>Distinción que importa para una cartera.</b> La frecuencia de "
        f"campañas por debajo de {_num(umbral)}% no difiere de forma "
        f"significativa entre fases ({sig_frec['k']} de {sig_frec['n']} "
        f"partidos, {_p(sig_frec['p'])}). El Niño desplaza hacia abajo el "
        f"centro de la distribución del {_cult(principal).lower()}, pero "
        f"no hay evidencia de que multiplique los eventos catastróficos.",
        E["cuerpo"]))

    # --- metodo ----------------------------------------------------------
    hist.append(Paragraph("5. Método y fuentes", E["h1"]))
    datos = [["Dato", "Fuente", "Cobertura"],
             ["ONI trimestral", "NOAA CPC (oni.ascii.txt)", "1950 - hoy"],
             ["Niño 3.4 mensual", "NOAA CPC (sstoi.indices)", "1982 - hoy"],
             ["Clima diario", "ERA5 vía Open-Meteo Archive",
              "1940 - hoy menos 5 d"],
             ["Rinde por partido", "MAGYP, estimaciones agrícolas (CKAN)",
              "1969/70 - 2024/25"],
             ["Geografía", "IGN vía API Georef", "—"]]
    hist.append(_tabla(datos, [3.6 * cm, 7.4 * cm, 4.8 * cm]))
    hist.append(Spacer(1, 9))

    hist.append(Paragraph("Secuencia del cálculo", E["h2"]))
    for i, txt in enumerate([
        "Cada lote se ubica por sus coordenadas y se resuelve el partido "
        "INDEC que lo contiene; ese código es el que empalma con la serie "
        "de rindes del MAGYP.",
        "Cada campaña se clasifica promediando el ONI sobre la ventana "
        "que cubre implantación y período crítico: JJA-JAS-ASO-SON "
        "para cultivos de invierno, SON-OND-NDJ-DJF para los de verano. "
        "Umbral CPC: |ONI| ≥ 0,5.",
        "El rinde se compara contra su tendencia tecnológica, estimada "
        "por media móvil centrada de 11 años con los bordes "
        "extrapolados por la pendiente local.",
        "Por fase se calculan desvío mediano, percentil 10 y frecuencia de "
        "campañas bajo el umbral de siniestro. La diferencia de medianas "
        "contra el resto de las campañas lleva intervalo de confianza del "
        "90% por bootstrap de 4.000 iteraciones.",
    ], start=1):
        hist.append(Paragraph(f"<b>{i}.</b> {txt}", E["cuerpo"]))

    # --- limitaciones ----------------------------------------------------
    hist.append(Paragraph("6. Limitaciones", E["h1"]))
    for tit, txt in [
        ("El rinde del MAGYP es de partido, no de lote",
         "Promedia manejos, suelos y fechas de siembra muy distintos. Es el "
         "mejor histórico largo disponible, pero subestima la varianza "
         "que ve una póliza individual."),
        ("ERA5 es reanálisis",
         "La precipitación es su variable más débil frente a "
         "eventos convectivos aislados, que es justamente lo que rompe un "
         "cultivo. Sirve para déficit hídrico estacional y para "
         "comparar campañas entre sí; no reemplaza al pluviómetro "
         "para peritar un siniestro."),
        ("Las ventanas fenológicas están calibradas para la pampa "
         "húmeda",
         "Son el parámetro más sensible de todo el análisis. "
         "Corridas sobre zonas con otras fechas de siembra requieren "
         "ajustarlas."),
        ("La fase de la campaña en curso es una proyección",
         f"Al cierre de este informe hay {cob['n']} de {cob['de']} trimestres "
         "de la ventana publicados. Si el evento se debilita antes de la "
         "primavera, el escenario cambia."),
        ("Muestra chica",
         "Entre 10 y 16 campañas por fase desde 1980. Por eso todo el "
         "informe reporta intervalos y no medias peladas."),
    ]:
        hist.append(Paragraph(tit, E["h2"]))
        hist.append(Paragraph(txt, E["cuerpo"]))

    # --- advertencia de alcance -----------------------------------------
    hist.append(KeepTogether([
        Paragraph("7. Qué NO mide este análisis", E["h1"]),
        _caja([
            "Este informe mide <b>desvío de rinde estacional</b>: el "
            "efecto del ENSO sobre cuánto produce un lote en la "
            "campaña, vía lluvia y temperatura en el período "
            "crítico.",
            "<b>Si la cobertura base de estas pólizas es granizo, ese no "
            "es el driver de siniestro.</b> El granizo es un evento convectivo "
            "localizado que puede arrasar un lote en una campaña de rinde "
            "perfectamente normal, y su correlación con el desvío "
            "estacional es prácticamente nula.",
            "La relación entre ENSO y convección severa existe y en "
            "el centro del país es fuerte, pero corre por otro mecanismo y "
            "<b>este análisis no la captura</b>. Para una cartera de "
            "granizo corresponde cruzar contra frecuencia de convección "
            "severa, no contra rinde.",
        ], ROJO, E),
    ]))

    # --- pie -------------------------------------------------------------
    def _pie(canvas, doc):
        canvas.saveState()
        canvas.setFont("Helvetica", 7.5)
        canvas.setFillColor(GRIS)
        canvas.drawString(2.2 * cm, 1.2 * cm,
                          f"Riesgo climático ENSO – cartera {campania}")
        canvas.drawRightString(A4[0] - 2.2 * cm, 1.2 * cm,
                               f"Página {doc.page}")
        canvas.setStrokeColor(colors.HexColor("#d5dbe1"))
        canvas.line(2.2 * cm, 1.55 * cm, A4[0] - 2.2 * cm, 1.55 * cm)
        canvas.restoreState()

    doc = SimpleDocTemplate(
        ruta, pagesize=A4, leftMargin=2.2 * cm, rightMargin=2.2 * cm,
        topMargin=2 * cm, bottomMargin=2 * cm,
        title=f"Riesgo climático ENSO - cartera {campania}",
        author="agroenso")
    doc.build(hist, onFirstPage=_pie, onLaterPages=_pie)
    return ruta
