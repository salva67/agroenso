# agroenso

Cruce de **fase ENSO × clima ERA5 × rinde por partido** para reportar cuánto
se mueve el rendimiento histórico según la etapa del fenómeno.

Se usa de dos maneras: una **web** (explorador estadístico público) y la
**CLI** original, que sigue intacta y agrega el módulo de pólizas.

Las fuentes son públicas, sin credenciales y con descarga directa. Nada de
scraping: todo sale de APIs o archivos de texto estables.

| Dato | Fuente | Endpoint | Cobertura |
|---|---|---|---|
| ONI trimestral | NOAA CPC | `cpc.ncep.noaa.gov/data/indices/oni.ascii.txt` | 1950 → hoy |
| Niño 3.4 mensual | NOAA CPC | `cpc.ncep.noaa.gov/data/indices/sstoi.indices` | 1982 → hoy |
| Clima diario | ERA5 vía Open-Meteo | `archive-api.open-meteo.com/v1/archive` | 1940 → hoy −5 d |
| Rinde por partido | MAGYP (CKAN) | `datos.magyp.gob.ar` → *estimaciones-agrícolas* | 1969/70 → 2024/25 |
| Centroides | IGN vía Georef | `apis.datos.gob.ar/georef/api` | — |
| Polígonos departamentales | IGN vía Georef | `infra.datos.gob.ar/georef/departamentos.geojson` | — |

---

## Arquitectura

La regla que ordena todo: **ninguna fuente externa se toca dentro de un
request HTTP.**

No es una preferencia de estilo. El CSV del MAGYP pesa 15 MB, Open-Meteo
corta por cuota horaria y un 429 suyo hace dormir el proceso hasta el cambio
de hora. Una web que dependa de eso en vivo no es servible. Así que el
pipeline está partido en dos mitades que se comunican por un archivo:

```
  ETL (batch, mensual)              snapshot              web (request)
  ───────────────────               ────────              ─────────────
  MAGYP  ─┐                                               FastAPI
  NOAA   ─┼─► normaliza ─────►  data/snapshot/*.parquet ──► en RAM ──► 20-30 ms
  Georef ─┤                         (1,2 MB)                  │
  ERA5   ─┘  (aparte, lento)        + geojson (0,5 MB)        └─► SVG en el navegador
                                    + clima (~4 MB)
```

**El snapshot es chico a propósito.** Todo el universo analizable —483
partidos × 41 cultivos × 1970-2024, 155.794 filas— son **1,2 MB en Parquet**,
más 0,5 MB de polígonos. Entra entero en memoria del proceso web. No hay base
de datos porque no hace falta una.

**Se precomputa el DATO, no el RESULTADO.** Los estadísticos por fase dependen
de `umbral`, `desde` y `metodo_tendencia`, que son justamente las perillas que
mueve quien lee el informe. Congelarlos en una tabla de resultados obligaría a
recalcular el snapshot entero por cada combinación. Con el dato normalizado en
RAM, calcular al vuelo cuesta 23 ms.

Para que eso cerrara hubo que sacar dos cuellos de botella del núcleo, que en
la CLI no molestaban y en una web sí:

| | antes | ahora |
|---|---|---|
| `resumen_por_fase` (bootstrap) | 781 ms | 19 ms |
| `fases_por_campania` | 84 ms | 13 ms |
| **consulta completa** | **863 ms** | **23 ms** |

El bootstrap era un loop de Python de 4000 iteraciones × 3 fases; ahora es un
remuestreo matricial. `fases_por_campania` filtraba el DataFrame del ONI 180
veces por partido para leer 4 valores; ahora hay un diccionario. Mismo método,
mismos números.

### Los módulos

| Módulo | Rol | ¿Sale a internet? |
|---|---|---|
| `agroenso/enso.py`, `clima.py`, `rindes.py`, `geo.py` | acceso a fuentes | sí, solo desde el ETL |
| `agroenso/cultivos.py` | ventanas fenológicas | no |
| `agroenso/analisis.py` | detrend y estadística | no |
| `agroenso/snapshot.py` | formato y frontera ETL ↔ web | no |
| `agroenso/consulta.py` | **motor de lectura**: índices en RAM | **nunca** |
| `etl/construir.py` | arma el snapshot | sí |
| `etl/clima.py` | ERA5 por partido, reanudable | sí |
| `web/api.py` | API HTTP | no |
| `web/static/` | frontend, SVG sin dependencias (mapa incluido) | no |

---

## Uso

### Web

```bash
pip install -r requirements-etl.txt -r requirements-web.txt
python -m etl.construir
uvicorn web.api:app --reload
```

En `http://localhost:8000`. Dos vistas:

- **Un partido** — la serie campaña a campaña con el desvío contra su
  tendencia, coloreada por fase; la diferencia de medianas por fase con IC 90 %;
  el ranking de sensibilidad climática; y las advertencias metodológicas que
  corresponden a *ese* resultado. Descargable en CSV.
- **Mapa por partido** (la vista que abre), con la nube de puntos debajo — para un cultivo y una fase, los
  483 partidos pintados por cuánto se desvía su rinde. El encuadre se ajusta
  solo a la zona donde el cultivo tiene serie: para maíz abarca de Salta a Río
  Negro, para caña se va al NOA. Arranca con maíz ya elegido, así lo primero
  que se ve es el mapa dibujado y no un selector vacío.

  Debajo, en su propia tarjeta: un **scatter de ONI contra desvío** con una
  campaña de un partido por punto, y al lado el **boxplot de esa misma nube
  por fase**. Comparten el eje vertical, así que se leen juntos — la nube
  muestra la dispersión, las cajas el resumen. Para maíz son 8.341 campañas
  de 194 partidos, con r² de 0,061: el gradiente de medianas es claro
  (Niña −7,1 %, Neutro +2,5 %, Niño +7,2 %) y los rangos intercuartiles se
  superponen mucho, que es exactamente lo que hay que ver.

La API está documentada sola en `/docs`. Los endpoints útiles:

```
GET /api/analisis?departamento_id=06721&cultivo=maiz&desde=1980&umbral=-15
GET /api/ranking?cultivo=maiz&fase=Nina&superficie_minima_ha=3000
GET /api/dispersion?cultivo=maiz&desde=1980  → la nube entera, sin resumir
GET /api/analisis.csv?...&tabla=resumen
GET /api/enso        /api/enso/fases        /api/partidos        /api/cultivos
GET /api/geo/departamentos  → polígonos en GeoJSON (gzip, cacheado 24 h)
GET /api/meta        → procedencia y fecha de corte del snapshot
```

### Clima (opcional, lento)

Sin esto la web funciona igual: el cruce ENSO × rinde está completo y solo
faltan las columnas climáticas y el ranking de sensibilidad.

```bash
python -m etl.clima --provincia "Buenos Aires"
python -m etl.clima --max-partidos 40      # una tanda y salir
```

Es **reanudable**: cada partido terminado se agrega al parquet y una corrida
nueva saltea los que ya están. Son ~500 pedidos a Open-Meteo con pausas y
backoff, así que lleva horas y puede necesitar varias corridas. Una sola
descarga por partido cubre todos sus cultivos.

### CLI

Sin cambios respecto de la versión anterior, y es la única que tiene el módulo
de pólizas:

```bash
python -m agroenso enso
python -m agroenso buscar salliquelo
python -m agroenso partido --depto-id 06721 --cultivo "soja total" --desde 1980
python -m agroenso plantilla polizas.csv
python -m agroenso polizas polizas.csv --umbral -20
python -m agroenso informe polizas.csv --campania 2026-2027
```

Las salidas (CSV con `;` y coma decimal, listos para Excel, más un PNG de tres
paneles y el PDF del informe) van a `salidas/`.

#### Padrón de pólizas

Columnas obligatorias: `poliza`, `cultivo`, y una ubicación — `lat`+`lon` del
lote, o `partido`, o `departamento_id` INDEC. Opcionales: `asegurado`,
`provincia`, `campania`, `superficie_ha`, `suma_asegurada`, `rinde_garantizado`,
`prima`. Los nombres se reconocen por alias (`nro_poliza`, `especie`, `latitud`,
`hectareas`, …); la lista completa está en `agroenso/polizas.py` → `ALIAS`.

Acepta CSV (detecta el separador solo) y XLSX.

---

## Deploy

### Instalar Docker en Windows (una sola vez)

Docker Desktop necesita WSL2, y las dos cosas requieren **consola como
administrador** y **un reinicio**. En una PowerShell elevada:

```powershell
wsl --install
```

Reiniciar. Después, otra vez en PowerShell como administrador:

```powershell
winget install --id Docker.DockerDesktop --exact --accept-package-agreements --accept-source-agreements
```

Abrir Docker Desktop una vez para que termine de configurarse, y comprobar
en una consola nueva:

```powershell
docker run --rm hello-world
```

> Si `docker` sigue sin reconocerse después de instalar, **abrí una consola
> nueva**: la que tenías abierta conserva el PATH de antes del instalador.
> Sin cerrarla: `$env:Path = [Environment]::GetEnvironmentVariable("Path","Machine")`.

### Construir y correr

```bash
docker build -t agroenso .
docker run -d --name agroenso -p 8000:8000 agroenso
```

Imagen: 644 MB. El ETL corre dentro del build y tarda ~13 s.

El build lleva dos chequeos que **fallan la construcción** en vez de dejar
pasar un contenedor roto:

- **`import web.api` en seco.** Atrapa una dependencia del ETL colada en la
  cadena de imports de la web. Pasó de verdad: `analisis` importaba `clima`,
  que importaba `requests`, que esta imagen no instala — el build daba verde y
  el contenedor arrancaba y moría.
- **`requests` no puede estar instalado.** Si alguien lo agrega a
  `requirements-web.txt` para "arreglar" un import, esto lo frena. El problema
  real es la cadena, no la dependencia faltante.

Por eso `requests` se importa **dentro** de las funciones que salen a la red y
no a nivel de módulo: así "la capa de lectura no toca la red" deja de ser una
promesa del README y queda sostenida por la estructura.

El build tiene dos etapas: la primera baja las fuentes y arma el snapshot, la
segunda solo sirve. **El dato queda horneado en la imagen**, no en un volumen.
Pesa unos pocos MB, así que no justifica infraestructura aparte, y a cambio la
imagen es inmutable: el contenedor en producción tiene el dato con el que fue
probado, y volver atrás es redeployar el tag anterior.

Refrescar el dato = reconstruir la imagen. `.github/workflows/snapshot.yml` lo
hace el día 8 de cada mes (el MAGYP publica mensualmente), verifica el contrato
y deja el `clima.parquet` como artefacto para no recalcularlo.

### Render (plan gratuito, sin tarjeta)

`render.yaml` está listo. El plan gratuito da **512 MB de RAM y 0,1 de CPU**,
y de ahí salen dos ajustes que no son opcionales:

- **`WEB_CONCURRENCY=1`.** Cada worker carga su propio pandas + pyarrow + el
  snapshot. Con uno solo el contenedor usa 176 MB de los 512 (verificado con
  `docker run --memory 512m`); con dos no entra.
- **`AGROENSO_PRECALENTAR=1`.** El ranking nacional recorre ~500 partidos y
  tarda medio segundo a CPU completa — con 0,1 de CPU son varios segundos, y
  le tocarían al primer visitante porque el mapa se dibuja solo al entrar. Se
  calcula en el arranque: la primera consulta pasa de ~500 ms a 39 ms.

El plan gratuito **duerme el servicio tras 15 min sin tráfico**; la primera
visita después de un rato espera 30-60 s. Es el precio de no poner tarjeta.

### Fly.io

`fly.toml` está listo, con región Buenos Aires y una máquina siempre
encendida. Da más RAM (512 MB reales con CPU completa) y no duerme, pero
**Fly pide una tarjeta al crear la organización**, aun para uso gratuito.

### Verificación

```bash
python verificar.py                          # la app en proceso
python verificar.py http://localhost:8000    # contra el contenedor
python verificar.py https://mi-deploy.dev    # contra el deploy
```

58 chequeos. No compara contra números fijos —el snapshot cambia todos los
meses y un test así se rompe solo—: valida invariantes. Que las fases sumen
las campañas, que los percentiles estén ordenados, que la clasificación
respete el umbral del CPC, que el detrend no deje sesgo, que no se escape un
`NaN` (que no es JSON válido), que todo partido del padrón tenga su polígono,
que mapa y ranking devuelvan los mismos números, y que los errores previstos
den 404 y no 500.

### Dos decisiones de color que no son cosméticas

**Las fases usan azul/gris/rojo**, que es la convención de cualquier gráfico de
ENSO publicado (azul = Niña fría, rojo = Niño cálido) y además es un esquema
divergente correcto: el medio lee como "nada".

**El mapa usa marrón/gris/verdeazulado**, deliberadamente distinto. Si el mapa
reusara el azul, un departamento azul leería "Niña" en vez de "sube el rinde".
Marrón↔verdeazulado es, encima, la convención de anomalía hídrica en agro. El
ranking comparte esa escala con el mapa porque miden lo mismo.

Las dos paletas pasaron el validador de daltonismo en modo claro y oscuro. En
el mapa, los dos escalones que rodean el neutro convergen —le pasa a toda
rampa divergente—, así que la banda neutra es ancha (±8 %), la leyenda lleva
los cortes numéricos y el tooltip da el valor exacto.

### Limpieza del dato del MAGYP

Dos correcciones que el ETL aplica y conviene conocer:

- **Se descartan 600 filas de agregados.** El MAGYP publica, por provincia,
  una fila con `departamento = "sin definir"` y código `XX000`: es la
  producción que no pudo asignarse a ningún partido. No es un partido, no
  tiene geometría ni centroide, y en una herramienta departamental solo
  ensucia el selector y el ranking. De ahí que sean 483 partidos y no 507.
- **Chascomús se remapea de `06217` a `06218`.** Cuando en 2009 se separó
  Lezama, el INDEC renumeró el partido; el MAGYP sigue publicando el código
  viejo. Sin esa línea, Chascomús se cae del mapa con 496 campañas a cuestas.
  La tabla está en `agroenso/geo.py` → `CORRECCION_ID`.

Con eso, los 483 partidos del padrón tienen los 483 polígonos: el verificador
lo chequea y falla si alguno se pierde.

---

## Método

1. **Clasificación de campañas.** Se promedia el ONI sobre la ventana que
   cubre implantación y período crítico: SON-OND-NDJ-DJF para cultivos de
   verano, JJA-JAS-ASO-SON para los de invierno. Umbral CPC: |ONI| ≥ 0,5.
2. **Ventana fenológica.** Cada cultivo tiene definida su ventana crítica de
   definición de rendimiento (`agroenso/cultivos.py`). Sobre ella se calcula
   lluvia, balance pp−ET0, racha seca máxima y días con Tmáx ≥ 33 / 35.
3. **Detrend.** El rinde se compara siempre contra su **tendencia
   tecnológica**, no en crudo. Sin esto se termina midiendo en qué década
   cayó cada fase ENSO, no el efecto del clima.
4. **Estadística de riesgo.** Por fase: desvío mediano, p10, y **frecuencia
   de campañas bajo el umbral de siniestro**. La diferencia de medianas
   contra el resto de las campañas viene con IC 90 % por bootstrap.
5. **Sensibilidad.** Ranking de variables climáticas por r² contra el desvío,
   para ver cuál merece entrar en un modelo y cuál no.

## Límites que conviene tener presentes

- **ERA5 es reanálisis.** La precipitación es su variable más débil frente a
  eventos convectivos aislados, que es justamente lo que rompe un cultivo.
  Sirve muy bien para déficit hídrico estacional y para comparar campañas
  entre sí. **No reemplaza al pluviómetro para peritar un siniestro.**
- **El rinde del MAGYP es de partido, no de lote.** Promedia manejos, suelos
  y fechas de siembra muy distintos. Es el mejor histórico largo que existe,
  pero subestima la varianza que ve una póliza individual.
- **Las ventanas fenológicas están calibradas para el oeste bonaerense.**
  Son el parámetro más sensible de todo el análisis. Para otras zonas se
  ajustan con `cultivos.cargar_ajustes('ventanas.yaml')`.
- **n chico.** Hay ~10-16 campañas por fase desde 1980. Por eso el resumen
  reporta IC bootstrap: en muchos partidos la diferencia de medianas cruza
  el cero y hay que decirlo. Donde el ENSO sí se ve con claridad es en la
  **cola** — la frecuencia de años malos —, no en la media. La web pone esa
  advertencia arriba del resultado, no en un README que nadie abre.
- **El centroide del partido no es el lote.** En partidos grandes hay 40-50 km
  de diferencia, y el gradiente de lluvia de la pampa es sensible a eso. La
  web usa el centroide porque razona por partido; la CLI acepta coordenadas
  del lote y hay que usarlas siempre que existan.
- **La web pide 15 campañas como mínimo.** Con menos, la media móvil del
  detrend sigue al dato en vez de a la tecnología, absorbe el efecto climático
  y los desvíos colapsan a cero: el informe mentiría por construcción, sin
  fallar.
- Correlación no es causalidad, y el ENSO explica una fracción de la
  variabilidad. `oni_medio` suele quedar último en el ranking de
  sensibilidad: actúa sobre el rinde a través de la lluvia y la temperatura,
  no directamente.

### Versiones fijas, y por qué

`requirements-web.txt` tiene versiones **exactas**, no rangos. Con
`pandas>=2.0` la imagen se armaba contra lo que hubiera ese día: el entorno
local corría 2.3.3 y el contenedor se llevó 3.0.6, que activa Copy-on-Write
por defecto. Ahí `Series.to_numpy()` pasa a devolver una vista de sólo lectura
en vez de una copia, y el relleno de bordes del detrend moría con
`assignment destination is read-only` — un bug que no se veía en desarrollo y
aparecía sólo al construir la imagen.

El código quedó compatible con las dos (pide la copia explícitamente) y está
verificado en ambas. Para subir una versión: cambiar el pin, `docker build`, y
correr `python verificar.py` contra el contenedor.

## Requisitos

- **Web**: `requirements-web.txt` — pandas, numpy, pyarrow, fastapi, uvicorn.
  Sin matplotlib ni openpyxl: los gráficos los dibuja el navegador en SVG.
- **ETL**: `requirements-etl.txt` — agrega `requests` y `pyyaml`.
- **CLI**: `requirements.txt` — el set original, con matplotlib y openpyxl.
