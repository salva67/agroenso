/* agroenso - frontend.
 *
 * Graficos en SVG a mano, sin libreria. Son tres formas y ninguna necesita
 * un motor generico; a cambio el contenedor no depende de ningun CDN, que
 * para un deploy en contenedor es lo que hace que la pagina funcione igual
 * sin salida a internet.
 *
 * Especificaciones de marca que se respetan aca (y conviene no romper):
 *   - extremo de dato redondeado 4px, anclado al cero; el otro extremo recto
 *   - 2px de aire entre barras vecinas, nunca un borde dibujado
 *   - lineas de IC de 2px, punto de >=8px con anillo de 2px del color de fondo
 *   - grilla y ejes en linea fina SOLIDA, un tono sobre la superficie
 *   - etiqueta directa solo en los extremos; el resto lo cuenta el tooltip
 *   - leyenda siempre presente y tabla de datos disponible: la identidad de
 *     fase nunca depende solo del color
 */
"use strict";

const $ = (s) => document.querySelector(s);
const $$ = (s) => Array.from(document.querySelectorAll(s));
const SVGNS = "http://www.w3.org/2000/svg";

const FASES = ["Nina", "Neutro", "Nino"];
const COLOR_FASE = { Nina: "var(--nina)", Neutro: "var(--neutro)", Nino: "var(--nino)" };

const estado = { analisis: null, ranking: null, dispersion: null,
                 cultivos: [], meta: null, geo: null, nombres: new Map() };

// --------------------------------------------------------------------------
// utilidades
// --------------------------------------------------------------------------

async function api(ruta, params) {
  const u = new URL(ruta, location.origin);
  for (const [k, v] of Object.entries(params || {})) {
    if (v !== null && v !== undefined && v !== "") u.searchParams.set(k, v);
  }
  const r = await fetch(u);
  const cuerpo = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(cuerpo.detail || `Error ${r.status}`);
  return cuerpo;
}

const num = (v, d = 1) =>
  v === null || v === undefined || Number.isNaN(v) ? "—" :
    v.toLocaleString("es-AR", { minimumFractionDigits: d, maximumFractionDigits: d });

const ent = (v) =>
  v === null || v === undefined || Number.isNaN(v) ? "—" :
    Math.round(v).toLocaleString("es-AR");

const firmado = (v, d = 1) =>
  v === null || v === undefined || Number.isNaN(v) ? "—" : (v > 0 ? "+" : "") + num(v, d);

function el(tag, attrs, hijos) {
  const n = document.createElementNS(SVGNS, tag);
  for (const [k, v] of Object.entries(attrs || {})) n.setAttribute(k, v);
  for (const h of hijos || []) n.appendChild(h);
  return n;
}

function texto(x, y, txt, clase, extra) {
  const t = el("text", { x, y, class: clase || "eje-txt", ...(extra || {}) });
  t.textContent = txt;
  return t;
}

/** Marcas de eje en numeros redondos, ~n divisiones. */
function ticks(min, max, n = 6) {
  if (!(max > min)) return [min];
  const crudo = (max - min) / n;
  const mag = Math.pow(10, Math.floor(Math.log10(crudo)));
  const paso = [1, 2, 2.5, 5, 10].map((x) => x * mag)
    .find((x) => x >= crudo) || 10 * mag;
  const out = [];
  for (let v = Math.ceil(min / paso) * paso; v <= max + 1e-9; v += paso) {
    out.push(Math.abs(v) < 1e-9 ? 0 : v);
  }
  return out;
}

/**
 * Barra con el extremo de DATO redondeado y el del cero recto.
 * Dibujar un rect con rx redondea las cuatro esquinas y la barra se despega
 * visualmente de la linea de base, que es justo lo que no se quiere.
 */
function barraPath(x, ancho, yCero, yDato, r = 4) {
  const arriba = yDato < yCero;
  const h = Math.abs(yDato - yCero);
  const rr = Math.min(r, ancho / 2, h);
  if (h < 0.6) return `M${x},${yCero}h${ancho}`;
  return arriba
    ? `M${x},${yCero}V${yDato + rr}a${rr},${rr} 0 0 1 ${rr},${-rr}h${ancho - 2 * rr}a${rr},${rr} 0 0 1 ${rr},${rr}V${yCero}Z`
    : `M${x},${yCero}V${yDato - rr}a${rr},${rr} 0 0 0 ${rr},${rr}h${ancho - 2 * rr}a${rr},${rr} 0 0 0 ${rr},${-rr}V${yCero}Z`;
}

// -- tooltip ---------------------------------------------------------------

const tip = $("#tooltip");

function mostrarTip(ev, titulo, fase, filas) {
  const punto = fase ? `<i class="punto f-${fase}"></i>` : "";
  tip.innerHTML =
    `<div class="t-tit">${punto}<span>${titulo}</span></div>` +
    filas.map(([k, v]) => `<div class="t-fila"><span>${k}</span><b>${v}</b></div>`).join("");
  tip.style.opacity = "1";
  moverTip(ev);
}

function moverTip(ev) {
  const r = tip.getBoundingClientRect();
  let x = ev.clientX + 14, y = ev.clientY - r.height - 12;
  if (x + r.width > innerWidth - 8) x = ev.clientX - r.width - 14;
  if (y < 8) y = ev.clientY + 18;
  tip.style.left = x + "px";
  tip.style.top = y + "px";
}

const ocultarTip = () => { tip.style.opacity = "0"; };

/** Engancha hover/foco a una marca. El hit target es la marca mas su aire. */
function hover(nodo, titulo, fase, filas) {
  nodo.addEventListener("mouseenter", (e) => mostrarTip(e, titulo, fase, filas));
  nodo.addEventListener("mousemove", moverTip);
  nodo.addEventListener("mouseleave", ocultarTip);
}

// --------------------------------------------------------------------------
// grafico 1: desvio por campania
// --------------------------------------------------------------------------

function graficoCampanias(svg, filas, umbral) {
  const W = svg.clientWidth || svg.parentNode.clientWidth || 860;
  const M = { t: 14, r: 14, b: 40, l: 46 };
  const H = 300;
  const pw = W - M.l - M.r, ph = H - M.t - M.b;
  svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
  svg.setAttribute("height", H);
  svg.replaceChildren();

  const datos = filas.filter((f) => f.desvio_pct !== null);
  if (!datos.length) return;

  const vals = datos.map((d) => d.desvio_pct);
  const lim = Math.max(Math.abs(Math.min(...vals, umbral)), Math.abs(Math.max(...vals)), 10) * 1.08;
  const y = (v) => M.t + ph * (1 - (v + lim) / (2 * lim));
  const paso = pw / datos.length;
  const ancho = Math.max(3, paso - 2);   // 2px de aire entre barras vecinas
  const x = (i) => M.l + i * paso + 1;

  const g = el("g");

  for (const t of ticks(-lim, lim, 6)) {
    g.appendChild(el("line", {
      class: t === 0 ? "cero" : "grilla",
      x1: M.l, x2: M.l + pw, y1: y(t), y2: y(t),
    }));
    g.appendChild(texto(M.l - 8, y(t) + 4, firmado(t, 0) + "%", "eje-txt", { "text-anchor": "end" }));
  }

  // Umbral de siniestro: la linea que define "campania mala". Va punteada a
  // proposito — es el unico trazo del grafico que NO es dato ni grilla.
  g.appendChild(el("line", {
    x1: M.l, x2: M.l + pw, y1: y(umbral), y2: y(umbral),
    stroke: "var(--alerta)", "stroke-width": 1.5, "stroke-dasharray": "5 4", opacity: .75,
  }));
  // Sin rotulo sobre la linea: caeria encima de las barras de esos anios y
  // la leyenda de arriba ya dice cual es el umbral.

  const peor = datos.reduce((a, b) => (b.desvio_pct < a.desvio_pct ? b : a));
  const mejor = datos.reduce((a, b) => (b.desvio_pct > a.desvio_pct ? b : a));

  datos.forEach((d, i) => {
    const fase = FASES.includes(d.fase) ? d.fase : "Neutro";
    const p = el("path", {
      class: "barra", d: barraPath(x(i), ancho, y(0), y(d.desvio_pct)),
      fill: COLOR_FASE[fase],
    });
    hover(p, d.campania || String(d.anio), fase, [
      ["Fase ENSO", `${d.fase || "sin dato"}${d.intensidad && d.intensidad !== "neutro" ? " " + d.intensidad : ""}`],
      ["ONI medio", num(d.oni_medio, 2)],
      ["Rinde", ent(d.rendimiento_kgxha) + " kg/ha"],
      ["Tendencia", ent(d.rinde_tendencia) + " kg/ha"],
      ["Desvio", firmado(d.desvio_pct) + " %"],
    ]);
    g.appendChild(p);

    // Etiqueta directa solo en los dos extremos. Un numero sobre cada barra
    // con 45 campanias es ilegible y no lo lee nadie.
    if (d === peor || d === mejor) {
      const arriba = d.desvio_pct > 0;
      g.appendChild(texto(x(i) + ancho / 2, y(d.desvio_pct) + (arriba ? -7 : 14),
        firmado(d.desvio_pct, 0) + "%", "etiqueta", { "text-anchor": "middle" }));
    }
  });

  // Eje x: la cantidad de etiquetas la manda el ancho disponible, no un
  // numero fijo. Cada "1995" necesita ~34px para no tocar a su vecina.
  const cada = Math.max(1, Math.ceil(datos.length / Math.max(3, Math.floor(pw / 46))));
  datos.forEach((d, i) => {
    if (i % cada) return;
    g.appendChild(texto(x(i) + ancho / 2, H - M.b + 18, String(d.anio),
      "eje-txt", { "text-anchor": "middle" }));
  });
  g.appendChild(texto(M.l, H - 6, "anio de siembra de la campania", "eje-titulo"));

  svg.appendChild(g);
}

// --------------------------------------------------------------------------
// grafico 2: diferencia de medianas con IC 90 %
// --------------------------------------------------------------------------

function graficoFases(svg, resumen) {
  const W = svg.clientWidth || svg.parentNode.clientWidth || 860;
  // Margen derecho ancho a proposito: las cifras van en una columna fija,
  // no pegadas a la punta de cada barra. Colgadas del extremo se pisan con
  // el rotulo de fase apenas el IC llega cerca del borde izquierdo.
  const M = { t: 16, r: 148, b: 40, l: 92 };
  const filas = FASES.map((f) => resumen.find((r) => r.fase === f)).filter(Boolean);
  const H = M.t + M.b + filas.length * 52;
  const pw = W - M.l - M.r;
  svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
  svg.setAttribute("height", H);
  svg.replaceChildren();
  if (!filas.length) return;

  const todos = filas.flatMap((r) => [r.ic90_inf, r.ic90_sup, r.dif_mediana_vs_resto])
    .filter((v) => v !== null && !Number.isNaN(v));
  const lim = Math.max(...todos.map(Math.abs), 5) * 1.15;
  const x = (v) => M.l + pw * (v + lim) / (2 * lim);
  const g = el("g");

  for (const t of ticks(-lim, lim, 6)) {
    g.appendChild(el("line", {
      class: t === 0 ? "cero" : "grilla",
      x1: x(t), x2: x(t), y1: M.t, y2: M.t + filas.length * 52 - 14,
    }));
    g.appendChild(texto(x(t), H - M.b + 20, firmado(t, 0) + "%", "eje-txt",
      { "text-anchor": "middle" }));
  }

  filas.forEach((r, i) => {
    const cy = M.t + i * 52 + 22;
    const cruza = r.ic90_inf !== null && r.ic90_inf < 0 && r.ic90_sup > 0;

    g.appendChild(texto(M.l - 14, cy + 4, r.fase, "etiqueta", { "text-anchor": "end" }));
    g.appendChild(texto(M.l - 14, cy + 18, `${r.campanias} camp.`, "eje-txt",
      { "text-anchor": "end" }));

    if (r.ic90_inf !== null) {
      g.appendChild(el("line", {
        class: "ic", x1: x(r.ic90_inf), x2: x(r.ic90_sup), y1: cy, y2: cy,
        stroke: COLOR_FASE[r.fase], opacity: cruza ? .45 : .8,
      }));
    }
    const p = el("circle", {
      class: "punto-dato", cx: x(r.dif_mediana_vs_resto), cy, r: 5.5,
      fill: COLOR_FASE[r.fase],
    });
    hover(p, r.fase, r.fase, [
      ["Campanias", r.campanias],
      ["Desvio mediano", firmado(r.desvio_mediano_pct) + " %"],
      ["Dif. vs resto", firmado(r.dif_mediana_vs_resto) + " %"],
      ["IC 90 %", `${firmado(r.ic90_inf)} a ${firmado(r.ic90_sup)} %`],
      ["Campanias bajo umbral", `${r.siniestros} de ${r.campanias} (${num(r.frec_siniestro_pct, 0)} %)`],
    ]);
    g.appendChild(p);

    // Etiqueta directa en las tres: son solo tres marcas y el numero ES el
    // resultado del informe. Van en columna, alineadas entre si.
    const cx = M.l + pw + 14;
    g.appendChild(texto(cx, cy + 1, firmado(r.dif_mediana_vs_resto) + " %",
      "etiqueta", { "text-anchor": "start" }));
    g.appendChild(texto(cx, cy + 16,
      cruza ? "el IC cruza el cero" : `IC ${firmado(r.ic90_inf, 0)} a ${firmado(r.ic90_sup, 0)}`,
      "eje-txt", { "text-anchor": "start",
                   fill: cruza ? "var(--alerta)" : "var(--texto-3)" }));
  });

  g.appendChild(texto(M.l, H - 6,
    "diferencia de la mediana de desvio contra el resto de las campanias",
    "eje-titulo"));
  svg.appendChild(g);
}

/** Rango real de campanias con dato, para no prometer anios que no existen. */
function rangoReal(campanias) {
  const a = campanias.filter((c) => c.desvio_pct !== null).map((c) => c.anio);
  return a.length ? [Math.min(...a), Math.max(...a)] : [null, null];
}


// --------------------------------------------------------------------------
// mapa: choropleth departamental
// --------------------------------------------------------------------------

/* Cortes de la escala divergente. La banda neutra es ANCHA (+-8 %) a
 * proposito: en cualquier divergente los dos escalones que rodean el centro
 * convergen, asi que meter ahi un -3 % y un +3 % con colores distintos
 * prometeria una diferencia que el ojo no puede leer. Con la banda ancha,
 * "casi cero" es una sola clase y honesta. */
const CORTES = [-30, -20, -8, 8, 20, 30];
const CLASES = ["var(--mapa-1)", "var(--mapa-2)", "var(--mapa-3)", "var(--mapa-4)",
                "var(--mapa-5)", "var(--mapa-6)", "var(--mapa-7)"];

function clase(v) {
  if (v === null || v === undefined || Number.isNaN(v)) return null;
  let i = 0;
  while (i < CORTES.length && v >= CORTES[i]) i++;
  return i;
}

const colorDe = (v) => { const i = clase(v); return i === null ? null : CLASES[i]; };

/** Proyeccion equirectangular con la x corregida por el coseno de la latitud.
 *
 * Sin esa correccion Argentina sale ~25 % mas ancha de lo que es: un grado de
 * longitud a -37 de latitud mide 80 % de lo que mide un grado de latitud. No
 * hace falta una conica: el error residual a esta escala no se ve, y evita
 * meter una libreria de proyecciones para dibujar un mapa de referencia.
 */
function proyector(bbox, ancho, alto, pad = 8) {
  const [lo0, la0, lo1, la1] = bbox;
  const k = Math.cos((la0 + la1) / 2 * Math.PI / 180);
  const w = (lo1 - lo0) * k, h = la1 - la0;
  const esc = Math.min((ancho - 2 * pad) / w, (alto - 2 * pad) / h);
  const dx = (ancho - w * esc) / 2, dy = (alto - h * esc) / 2;
  return (lon, lat) => [dx + (lon - lo0) * k * esc, dy + (la1 - lat) * esc];
}

function anillosAPath(coords, tipo, pr) {
  const anillos = tipo === "Polygon" ? coords : coords.flat();
  let d = "";
  for (const anillo of anillos) {
    if (!anillo.length) continue;
    for (let i = 0; i < anillo.length; i++) {
      const [x, y] = pr(anillo[i][0], anillo[i][1]);
      d += (i ? "L" : "M") + x.toFixed(1) + "," + y.toFixed(1);
    }
    d += "Z";
  }
  return d;
}

function graficoMapa(svg, geo, mapa, fase, nombres) {
  svg.replaceChildren();
  if (!geo || !mapa) return;

  const val = new Map(mapa.map((r) => [r[0], r]));

  // El encuadre lo fijan los departamentos CON dato, no el pais entero: para
  // maiz eso centra la pampa en vez de gastar media pantalla en Patagonia,
  // y para cania de azucar se va solo al NOA.
  let lo0 = 999, la0 = 999, lo1 = -999, la1 = -999;
  for (const f of geo.features) {
    if (!val.has(f.properties.id)) continue;
    const rec = (o) => {
      if (typeof o[0] === "number") {
        lo0 = Math.min(lo0, o[0]); lo1 = Math.max(lo1, o[0]);
        la0 = Math.min(la0, o[1]); la1 = Math.max(la1, o[1]);
      } else o.forEach(rec);
    };
    rec(f.geometry.coordinates);
  }
  if (lo0 > lo1) return;
  const mg = Math.max((lo1 - lo0), (la1 - la0)) * 0.04;
  const bbox = [lo0 - mg, la0 - mg, lo1 + mg, la1 + mg];

  // El lienzo se dimensiona DESPUES de conocer el encuadre, no antes. Con un
  // alto fijo, un recorte alto y angosto como la pampa humeda deja cientos de
  // pixeles muertos a los costados; derivandolo del bbox el mapa queda justo.
  const disp = svg.parentNode.clientWidth || 420;
  const k = Math.cos((bbox[1] + bbox[3]) / 2 * Math.PI / 180);
  const aspecto = ((bbox[2] - bbox[0]) * k) / (bbox[3] - bbox[1]);
  const H = Math.round(Math.max(320, Math.min(640, disp / aspecto)));
  const W = Math.round(Math.min(disp, H * aspecto));
  svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
  svg.setAttribute("width", W);
  svg.setAttribute("height", H);
  const pr = proyector(bbox, W, H);

  // Los partidos sin dato van primero y en gris: dan contexto geografico sin
  // competir con el dato. Sin ellos el mapa flota sin referencia.
  const gSin = el("g"), gCon = el("g");
  for (const f of geo.features) {
    const id = f.properties.id;
    const d = anillosAPath(f.geometry.coordinates, f.geometry.type, pr);
    if (!d) continue;
    const r = val.get(id);
    if (!r) {
      gSin.appendChild(el("path", { class: "depto-sin", d }));
      continue;
    }
    const p = el("path", { class: "depto", d, fill: colorDe(r[1]) });
    hover(p, nombres.get(id) || id, null, [
      [`Campanias en ${fase}`, r[3]],
      ["Desvio mediano", firmado(r[1]) + " %"],
      ["Campanias bajo umbral", num(r[2], 0) + " %"],
    ]);
    gCon.appendChild(p);
  }
  svg.appendChild(gSin);
  svg.appendChild(gCon);
}

/** Leyenda con los cortes numericos.
 *
 * No es decorativa: es el relieve que exige una rampa divergente. Los dos
 * escalones junto al neutro son casi iguales a la vista, asi que el lector
 * necesita el numero, aca y en el tooltip. */
function leyendaMapa(nodo, fase) {
  const filas = [];
  for (let i = CLASES.length - 1; i >= 0; i--) {
    const lo = i === 0 ? null : CORTES[i - 1];
    const hi = i === CLASES.length - 1 ? null : CORTES[i];
    const txt = lo === null ? `menos de ${hi} %`
      : hi === null ? `mas de +${lo} %`
      : `${firmado(lo, 0)} a ${firmado(hi, 0)} %`;
    filas.push(`<div class="fila"><i class="chip" style="background:${CLASES[i]}"></i><b>${txt}</b></div>`);
  }
  nodo.innerHTML =
    `<div class="titulo">Desvio del rinde en ${fase}</div>` + filas.join("") +
    `<div class="fila" style="margin-top:7px"><i class="chip" style="background:var(--mapa-sin)"></i>sin serie suficiente</div>`;
}


// --------------------------------------------------------------------------
// scatter ONI x desvio + boxplot por fase, con eje Y compartido
// --------------------------------------------------------------------------

/** Circulo como subtrazo de un path compuesto.
 *
 *  Son ~8.000 puntos: un <circle> por cada uno hincha el DOM y el navegador
 *  se arrastra al redibujar. Tres paths compuestos —uno por fase— dibujan lo
 *  mismo y el color sigue codificando la fase.
 */
function circuloPath(cx, cy, r) {
  return `M${(cx - r).toFixed(1)},${cy.toFixed(1)}`
    + `a${r},${r} 0 1,0 ${2 * r},0a${r},${r} 0 1,0 ${-2 * r},0Z`;
}

function graficoDispersion(svg, d) {
  const W = svg.clientWidth || svg.parentNode.clientWidth || 860;
  const H = 400;
  const M = { t: 16, r: 14, b: 48, l: 52 };
  // El boxplot ocupa una franja fija a la derecha: son tres cajas, no necesita
  // mas, y el scatter aprovecha todo lo demas.
  const anchoCaja = Math.min(200, Math.max(130, W * 0.26));
  const sep = 26;
  const pwS = W - M.l - M.r - anchoCaja - sep;
  const ph = H - M.t - M.b;
  svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
  svg.setAttribute("height", H);
  svg.replaceChildren();
  if (!d || !d.n) return;

  // Escala Y COMPARTIDA por los dos paneles: es lo que permite leerlos juntos.
  // Se recorta a los percentiles 1 y 99 para que un punado de campanias
  // catastroficas no aplaste el resto de la nube contra el cero.
  const ys = d.desvio.slice().sort((a, b) => a - b);
  const q = (p) => ys[Math.min(ys.length - 1, Math.floor(p * ys.length))];
  const lim = Math.max(Math.abs(q(0.01)), Math.abs(q(0.99))) * 1.08;
  const y = (v) => M.t + ph * (1 - (v + lim) / (2 * lim));

  const xs = d.oni;
  const xlo = Math.min(...xs) - 0.15, xhi = Math.max(...xs) + 0.15;
  const x = (v) => M.l + pwS * (v - xlo) / (xhi - xlo);

  const g = el("g");

  // grilla horizontal, comun a los dos paneles
  for (const t of ticks(-lim, lim, 6)) {
    g.appendChild(el("line", { class: t === 0 ? "cero" : "grilla",
      x1: M.l, x2: W - M.r, y1: y(t), y2: y(t) }));
    g.appendChild(texto(M.l - 8, y(t) + 4, firmado(t, 0) + "%", "eje-txt",
      { "text-anchor": "end" }));
  }

  // -- panel izquierdo: la nube ------------------------------------------
  const porFase = d.fases_orden.map(() => []);
  for (let i = 0; i < d.oni.length; i++) {
    if (Math.abs(d.desvio[i]) > lim) continue;      // recortado por la escala
    porFase[d.fase[i]].push(circuloPath(x(d.oni[i]), y(d.desvio[i]), 2.4));
  }
  d.fases_orden.forEach((f, i) => {
    if (!porFase[i].length) return;
    g.appendChild(el("path", { class: "nube", d: porFase[i].join(""),
                               fill: COLOR_FASE[f] }));
  });

  // recta de ajuste: no es un modelo, es para ver el signo y lo poco que explica
  const a0 = d.ajuste.ordenada + d.ajuste.pendiente * xlo;
  const a1 = d.ajuste.ordenada + d.ajuste.pendiente * xhi;
  g.appendChild(el("path", { class: "ajuste",
    d: `M${x(xlo)},${y(Math.max(-lim, Math.min(lim, a0)))}`
       + `L${x(xhi)},${y(Math.max(-lim, Math.min(lim, a1)))}`,
    stroke: "var(--texto)", opacity: .55 }));

  for (const t of ticks(xlo, xhi, 6)) {
    g.appendChild(texto(x(t), H - M.b + 20, firmado(t, 1), "eje-txt",
      { "text-anchor": "middle" }));
  }
  g.appendChild(texto(M.l + pwS / 2, H - M.b + 40,
    "ONI medio de la ventana de la campania", "eje-titulo",
    { "text-anchor": "middle" }));
  g.appendChild(texto(M.l - 40, M.t + ph / 2,
    "desvio del rinde contra su tendencia (%)", "eje-titulo",
    { "text-anchor": "middle", transform: `rotate(-90 ${M.l - 40} ${M.t + ph / 2})` }));

  // -- panel derecho: las cajas -------------------------------------------
  const x0 = M.l + pwS + sep;
  g.appendChild(el("line", { class: "panel-sep",
    x1: x0 - sep / 2, x2: x0 - sep / 2, y1: M.t, y2: M.t + ph }));

  const cajas = d.fases_orden.map((f) => d.caja.find((c) => c.fase === f))
    .filter((c) => c && c.n);
  const paso = anchoCaja / Math.max(1, cajas.length);
  const ancho = Math.min(46, paso * 0.56);

  cajas.forEach((c, i) => {
    const cx = x0 + paso * (i + 0.5);
    const col = COLOR_FASE[c.fase];
    // bigotes
    g.appendChild(el("line", { class: "bigote", stroke: col,
      x1: cx, x2: cx, y1: y(c.bigote_inf), y2: y(c.bigote_sup), opacity: .7 }));
    for (const v of [c.bigote_inf, c.bigote_sup]) {
      g.appendChild(el("line", { class: "bigote", stroke: col, opacity: .7,
        x1: cx - ancho / 4, x2: cx + ancho / 4, y1: y(v), y2: y(v) }));
    }
    // caja
    const rect = el("rect", { class: "caja-borde", stroke: col, fill: col,
      x: cx - ancho / 2, width: ancho,
      y: y(c.q3), height: Math.max(1, y(c.q1) - y(c.q3)), rx: 3 });
    hover(rect, c.fase, c.fase, [
      ["Campanias", c.n],
      ["Mediana", firmado(c.mediana) + " %"],
      ["Rango intercuartil", `${firmado(c.q1)} a ${firmado(c.q3)} %`],
      ["Bigotes (1,5 RIC)", `${firmado(c.bigote_inf)} a ${firmado(c.bigote_sup)} %`],
      ["Fuera de bigotes", c.atipicos],
    ]);
    g.appendChild(rect);
    g.appendChild(el("line", { class: "caja-mediana", stroke: col,
      x1: cx - ancho / 2, x2: cx + ancho / 2, y1: y(c.mediana), y2: y(c.mediana) }));
    g.appendChild(texto(cx, H - M.b + 20, c.fase, "etiqueta",
      { "text-anchor": "middle" }));
    g.appendChild(texto(cx, H - M.b + 34, `n=${c.n}`, "eje-txt",
      { "text-anchor": "middle" }));
  });

  svg.appendChild(g);
}

// --------------------------------------------------------------------------
// tablas
// --------------------------------------------------------------------------

function tabla(nodo, columnas, filas) {
  const th = columnas.map((c) =>
    `<th class="${c.txt ? "txt" : ""}">${c.tit}</th>`).join("");
  const tb = filas.map((f) =>
    "<tr>" + columnas.map((c) =>
      `<td class="${c.txt ? "txt" : ""}">${c.fmt(f)}</td>`).join("") + "</tr>").join("");
  nodo.innerHTML = `<thead><tr>${th}</tr></thead><tbody>${tb}</tbody>`;
}

// --------------------------------------------------------------------------
// vista: partido
// --------------------------------------------------------------------------

function pintarPartido(a) {
  estado.analisis = a;
  const res = a.resumen_fase;
  const nina = res.find((r) => r.fase === "Nina");
  const nino = res.find((r) => r.fase === "Nino");
  const total = a.campanias.filter((c) => c.desvio_pct !== null).length;
  // El `hasta` del pedido es el anio en curso; el dato del MAGYP llega uno o
  // dos anios antes. Mostrar el rango pedido y no el real invita a creer que
  // la campania de este anio esta adentro.
  const [r0, r1] = rangoReal(a.campanias);

  $("#p-tiles").innerHTML = [
    ["Campanias analizadas", total, `${r0}–${r1}`],
    ["Desvio mediano en Nina", nina ? firmado(nina.desvio_mediano_pct, 0) + " %" : "—",
      nina ? `${nina.campanias} campanias` : ""],
    ["Desvio mediano en Nino", nino ? firmado(nino.desvio_mediano_pct, 0) + " %" : "—",
      nino ? `${nino.campanias} campanias` : ""],
    ["Campanias bajo umbral en Nina", nina ? num(nina.frec_siniestro_pct, 0) + " %" : "—",
      nina ? `${nina.siniestros} de ${nina.campanias}` : ""],
  ].map(([r, c, p]) =>
    `<div class="tile"><div class="rotulo">${r}</div><div class="cifra">${c}</div><div class="pie">${p}</div></div>`
  ).join("");

  $("#ley-umbral").textContent = `umbral de siniestro ${firmado(a.parametros.umbral_siniestro_pct, 0)} %`;

  // Mostrar ANTES de dibujar: dentro de un contenedor con `hidden` el SVG
  // mide 0 de ancho, el grafico cae al ancho por defecto y despues queda
  // estirado por el viewBox, con el texto escalado.
  $("#p-estado").hidden = true;
  $("#p-contenido").hidden = false;

  graficoCampanias($("#g-campanias"), a.campanias, a.parametros.umbral_siniestro_pct);
  graficoFases($("#g-fases"), res);

  tabla($("#t-campanias"), [
    { tit: "Campania", txt: true, fmt: (f) => f.campania || f.anio },
    { tit: "Fase", txt: true, fmt: (f) => `<i class="punto f-${FASES.includes(f.fase) ? f.fase : "Neutro"}"></i> ${f.fase || "—"}` },
    { tit: "ONI", fmt: (f) => num(f.oni_medio, 2) },
    { tit: "Rinde kg/ha", fmt: (f) => ent(f.rendimiento_kgxha) },
    { tit: "Tendencia", fmt: (f) => ent(f.rinde_tendencia) },
    { tit: "Desvio %", fmt: (f) => firmado(f.desvio_pct) },
    { tit: "Sup. sembrada ha", fmt: (f) => ent(f.superficie_sembrada_ha) },
    { tit: "No cosechada %", fmt: (f) => num(f.perdida_cosecha_pct, 1) },
  ], a.campanias);

  tabla($("#t-fases"), [
    { tit: "Fase", txt: true, fmt: (f) => `<i class="punto f-${f.fase}"></i> ${f.fase}` },
    { tit: "Campanias", fmt: (f) => f.campanias },
    { tit: "Desvio mediano %", fmt: (f) => firmado(f.desvio_mediano_pct) },
    { tit: "p10 %", fmt: (f) => firmado(f.p10_pct) },
    { tit: "p90 %", fmt: (f) => firmado(f.p90_pct) },
    { tit: "Bajo umbral", fmt: (f) => `${f.siniestros} / ${f.campanias}` },
    { tit: "Frec. %", fmt: (f) => num(f.frec_siniestro_pct, 0) },
    { tit: "Dif. vs resto %", fmt: (f) => firmado(f.dif_mediana_vs_resto) },
    { tit: "IC 90 %", fmt: (f) => `${firmado(f.ic90_inf)} a ${firmado(f.ic90_sup)}` },
  ], FASES.map((f) => res.find((r) => r.fase === f)).filter(Boolean));

  const cs = $("#c-sensibilidad");
  if (a.sensibilidad && a.sensibilidad.length) {
    cs.hidden = false;
    tabla($("#t-sensibilidad"), [
      { tit: "Variable", txt: true, fmt: (f) => f.descripcion },
      { tit: "n", fmt: (f) => f.n },
      { tit: "r", fmt: (f) => num(f.r_pearson, 3) },
      { tit: "r2", fmt: (f) => num(f.r2, 3) },
      { tit: "% por unidad", fmt: (f) => num(f.pendiente_pct_por_unidad, 3) },
    ], a.sensibilidad);
  } else {
    cs.hidden = true;
  }

  $("#p-avisos").innerHTML = a.advertencias.map((t) => `<li>${t}</li>`).join("");
}

function paramsPartido() {
  return {
    departamento_id: $("#f-partido").value,
    cultivo: $("#f-cultivo").value,
    desde: $("#f-desde").value,
    umbral: $("#f-umbral").value,
    tendencia: $("#f-tendencia").value,
  };
}

async function consultarPartido() {
  const p = paramsPartido();
  if (!p.departamento_id || !p.cultivo) return;
  $("#p-contenido").hidden = true;
  $("#p-estado").hidden = false;
  $("#p-estado").className = "estado";
  $("#p-estado").textContent = "Calculando…";
  try {
    pintarPartido(await api("/api/analisis", p));
  } catch (e) {
    $("#p-estado").className = "estado error";
    $("#p-estado").textContent = e.message;
  }
}

// --------------------------------------------------------------------------
// vista: ranking
// --------------------------------------------------------------------------

async function consultarRanking() {
  const cultivo = $("#r-cultivo").value;
  if (!cultivo) return;
  $("#r-contenido").hidden = true;
  $("#r-estado").hidden = false;
  $("#r-estado").className = "estado";
  $("#r-estado").textContent = "Calculando…";
  try {
    const d = await api("/api/ranking", {
      cultivo,
      fase: $("#r-fase").value,
      provincia: $("#r-provincia").value,
      desde: $("#r-desde").value,
      umbral: $("#r-umbral").value,
      superficie_minima_ha: $("#r-superficie").value,
    });
    estado.ranking = d;
    $("#m-titulo").textContent = `${d.cultivo} por partido — fase ${d.fase}`;
    leyendaMapa($("#m-escala"), d.fase);
    // Una API mas vieja que este frontend no manda `mapa`. Sin esta guarda,
    // eso rompe la vista entera en vez de perder solo el mapa.
    $("#m-pie").textContent = d.mapa
      ? `${d.mapa.length} partidos medidos. Pasar el mouse por encima para ver `
        + `el valor exacto; el detalle campania a campania esta en la otra pestania.`
      : "Esta version de la API no devuelve datos para el mapa.";
    $("#r-estado").hidden = true;
    $("#r-contenido").hidden = false;
    if (d.mapa) {
      await cargarGeo();
      graficoMapa($("#g-mapa"), estado.geo, d.mapa, d.fase, estado.nombres);
    }
    // La nube va aparte y despues: el mapa es lo que se mira primero, y esta
    // consulta trae ~8.000 puntos. Si falla, el mapa ya esta en pantalla.
    cargarDispersion();
  } catch (e) {
    $("#r-estado").className = "estado error";
    $("#r-estado").textContent = e.message;
  }
}

// --------------------------------------------------------------------------
// carga de catalogos
// --------------------------------------------------------------------------

/** Nube de puntos del cultivo elegido. Usa los mismos filtros que el mapa,
 *  salvo la fase: el scatter y el boxplot muestran las tres a la vez, que es
 *  justamente para lo que sirven. */
async function cargarDispersion() {
  const cultivo = $("#r-cultivo").value;
  if (!cultivo) return;
  try {
    const d = await api("/api/dispersion", {
      cultivo,
      provincia: $("#r-provincia").value,
      desde: $("#r-desde").value,
      superficie_minima_ha: $("#r-superficie").value,
    });
    estado.dispersion = d;
    $("#d-titulo").textContent =
      `${cultivo}: ONI contra desvio del rinde, campania por campania`;
    $("#d-ajuste").textContent =
      `recta de ajuste: ${firmado(d.ajuste.pendiente)} % de rinde por punto de `
      + `ONI · r² ${num(d.ajuste.r2, 3)} · ${d.n.toLocaleString("es-AR")} `
      + `campanias de ${d.partidos} partidos`;
    tabla($("#t-caja"), [
      { tit: "Fase", txt: true,
        fmt: (f) => `<i class="punto f-${f.fase}"></i> ${f.fase}` },
      { tit: "Campanias", fmt: (f) => f.n },
      { tit: "Mediana %", fmt: (f) => firmado(f.mediana) },
      { tit: "Media %", fmt: (f) => firmado(f.media) },
      { tit: "q1 %", fmt: (f) => firmado(f.q1) },
      { tit: "q3 %", fmt: (f) => firmado(f.q3) },
      { tit: "Bigote inf %", fmt: (f) => firmado(f.bigote_inf) },
      { tit: "Bigote sup %", fmt: (f) => firmado(f.bigote_sup) },
      { tit: "Fuera de bigotes", fmt: (f) => f.atipicos },
    ], d.fases_orden.map((x) => d.caja.find((c) => c.fase === x))
        .filter((c) => c && c.n));
    graficoDispersion($("#g-dispersion"), d);
  } catch (e) {
    estado.dispersion = null;
    $("#d-titulo").textContent = "No se pudo calcular la nube de puntos";
    $("#d-ajuste").textContent = e.message;
  }
}

/** El geojson son ~500 KB: se baja una sola vez, la primera que hace falta.
 *  Si falla, el ranking y la tabla siguen funcionando sin mapa. */
async function cargarGeo() {
  if (estado.geo !== null) return;
  try {
    estado.geo = await api("/api/geo/departamentos");
  } catch (e) {
    estado.geo = false;
    $("#m-pie").textContent = "No se pudo cargar el mapa: " + e.message;
  }
}

async function cargarEnso() {
  try {
    const e = await api("/api/enso");
    $("#enso-oni").textContent = firmado(e.oni, 1);
    $("#enso-trim").textContent = `${e.trimestre} ${e.anio} · ${e.tendencia}`;
    $("#enso-fase").textContent = e.fase;
    $("#enso-int").textContent = e.intensidad;
    $("#enso-punto").className = `punto f-${e.fase}`;
    $("#enso-tile").hidden = false;
  } catch { /* el tile es contexto, no dato del informe: si falla, se omite */ }
}

async function cargarMeta() {
  try {
    const m = await api("/api/meta");
    estado.meta = m;
    const f = (m.construido || "").slice(0, 10);
    $("#pie-corte").textContent =
      `Snapshot del ${f} · campanias hasta ${m.fuentes?.campania_max ?? "—"} · ` +
      `ONI hasta ${m.fuentes?.oni_ultimo ?? "—"}`;
  } catch { /* nada */ }
}

async function cargarNombres() {
  // El geojson solo trae `id`; el nombre para el tooltip sale de aca.
  try {
    for (const p of await api("/api/partidos", { limite: 2000 })) {
      estado.nombres.set(p.departamento_id, `${p.departamento} — ${p.provincia}`);
    }
  } catch { /* el tooltip cae al id, que sigue siendo identificable */ }
}

async function cargarProvincias() {
  const ps = await api("/api/provincias");
  const opts = `<option value="">Elegir…</option>` +
    ps.map((p) => `<option value="${p.provincia}">${p.provincia} (${p.partidos})</option>`).join("");
  $("#f-provincia").innerHTML = opts;
  $("#r-provincia").innerHTML = `<option value="">Todas</option>` +
    ps.map((p) => `<option value="${p.provincia}">${p.provincia}</option>`).join("");
}

async function cargarPartidos() {
  const prov = $("#f-provincia").value;
  const sel = $("#f-partido");
  if (!prov) { sel.innerHTML = `<option value="">—</option>`; return; }
  const ps = await api("/api/partidos", { provincia: prov, limite: 600 });
  sel.innerHTML = `<option value="">Elegir…</option>` +
    ps.map((p) => `<option value="${p.departamento_id}">${p.departamento}</option>`).join("");
}

async function cargarCultivosDelPartido() {
  const dep = $("#f-partido").value;
  const sel = $("#f-cultivo");
  if (!dep) { sel.innerHTML = `<option value="">—</option>`; return; }
  try {
    const cs = await api("/api/cultivos", { departamento_id: dep, minimo: 15 });
    sel.innerHTML = `<option value="">Elegir…</option>` + cs.map((c) =>
      `<option value="${c.cultivo}">${c.cultivo} (${c.campanias} camp.)</option>`).join("");
  } catch {
    sel.innerHTML = `<option value="">sin cultivos con serie larga</option>`;
  }
}

async function cargarCatalogoCultivos() {
  const cs = await api("/api/cultivos");
  estado.cultivos = cs;
  // Solo los que tienen ventana fenologica y presencia amplia: un ranking
  // nacional de yerba mate no significa nada.
  const utiles = cs.filter((c) => c.ventana_fenologica && c.partidos_n >= 20);
  $("#r-cultivo").innerHTML = `<option value="">Elegir…</option>` +
    utiles.map((c) => `<option value="${c.cultivo}">${c.cultivo} (${c.partidos_n} partidos)</option>`).join("");
}

// --------------------------------------------------------------------------
// arranque
// --------------------------------------------------------------------------

// Cultivo con el que arranca el mapa. Que la vista principal pida elegir algo
// antes de mostrar nada hacia que la app pareciera no tener mapa: se abria en
// la otra pestania y esta quedaba en blanco hasta el segundo clic.
const CULTIVO_INICIAL = "maiz";

function pestania(cual) {
  const esPartido = cual === "partido";
  $("#tab-partido").setAttribute("aria-selected", String(esPartido));
  $("#tab-ranking").setAttribute("aria-selected", String(!esPartido));
  $("#vista-partido").hidden = !esPartido;
  $("#vista-ranking").hidden = esPartido;
  // La vista que acaba de aparecer ya tiene ancho: recien ahora se puede
  // dibujar a la medida real.
  requestAnimationFrame(redibujar);
}

function redibujar() {
  if (estado.analisis && !$("#vista-partido").hidden) {
    graficoCampanias($("#g-campanias"), estado.analisis.campanias,
      estado.analisis.parametros.umbral_siniestro_pct);
    graficoFases($("#g-fases"), estado.analisis.resumen_fase);
  }
  if (estado.ranking && !$("#vista-ranking").hidden) {
    if (estado.geo) {
      graficoMapa($("#g-mapa"), estado.geo, estado.ranking.mapa,
                  estado.ranking.fase, estado.nombres);
    }
    if (estado.dispersion) {
      graficoDispersion($("#g-dispersion"), estado.dispersion);
    }
  }
}

/** Deja elegido un cultivo razonable y dispara la primera consulta. */
async function arranqueMapa() {
  const sel = $("#r-cultivo");
  const hay = Array.from(sel.options).map((o) => o.value);
  const elegido = hay.includes(CULTIVO_INICIAL) ? CULTIVO_INICIAL
    : hay.find((v) => v) || "";
  if (!elegido) {
    $("#r-estado").textContent = "El snapshot no trae cultivos con serie suficiente.";
    return;
  }
  sel.value = elegido;
  await consultarRanking();
}

function descargar(tipo) {
  const p = paramsPartido();
  if (!p.departamento_id || !p.cultivo) return;
  const u = new URL("/api/analisis.csv", location.origin);
  for (const [k, v] of Object.entries({ ...p, tabla: tipo })) u.searchParams.set(k, v);
  location.href = u.toString();
}

function init() {
  $("#tab-partido").onclick = () => pestania("partido");
  $("#tab-ranking").onclick = () => pestania("ranking");

  $("#f-provincia").onchange = async () => {
    await cargarPartidos();
    await cargarCultivosDelPartido();
  };
  $("#f-partido").onchange = async () => {
    await cargarCultivosDelPartido();
    consultarPartido();
  };
  for (const s of ["#f-cultivo", "#f-desde", "#f-umbral", "#f-tendencia"]) {
    $(s).onchange = consultarPartido;
  }
  for (const s of ["#r-cultivo", "#r-fase", "#r-provincia", "#r-desde",
                   "#r-umbral", "#r-superficie"]) {
    $(s).onchange = consultarRanking;
  }
  $("#btn-csv-camp").onclick = () => descargar("campanias");
  $("#btn-csv-res").onclick = () => descargar("resumen");

  let t;
  const pedirRedibujo = () => { clearTimeout(t); t = setTimeout(redibujar, 120); };
  addEventListener("resize", pedirRedibujo);

  // Los graficos se dimensionan segun el ancho de su tarjeta. Si la pagina
  // carga con el contenedor en cero —pestania de fondo, panel plegado,
  // ventana minimizada— salen dibujados en miniatura y ahi se quedan, porque
  // el evento `resize` de window nunca llega: lo que cambio fue la tarjeta,
  // no la ventana. El observer redibuja cuando la tarjeta recupera ancho.
  //
  // Observa las TARJETAS, no los <svg>: redibujar cambia el tamanio del svg y
  // observarlo a el realimentaria el bucle. El ancho de la tarjeta lo fija el
  // layout, asi que no depende de lo que haya adentro.
  if (window.ResizeObserver) {
    const ro = new ResizeObserver((entradas) => {
      if (entradas.some((e) => e.contentRect.width > 0)) pedirRedibujo();
    });
    for (const c of $$(".tarjeta")) ro.observe(c);
  }

  cargarEnso();
  cargarMeta();
  cargarProvincias();
  cargarNombres();
  // El mapa se dibuja solo al abrir: nombres primero (los necesita el
  // tooltip), despues el catalogo, y arranca la consulta sin esperar clics.
  cargarCatalogoCultivos().then(arranqueMapa);
}

init();
