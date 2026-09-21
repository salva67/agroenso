# Imagen de la web. Dos etapas: una construye el snapshot, la otra lo sirve.
#
# El snapshot se hornea EN la imagen en vez de montarse en un volumen. Pesa
# 1-6 MB, con lo cual no justifica infraestructura aparte, y a cambio la
# imagen queda inmutable: el contenedor que corre en produccion tiene el dato
# con el que fue probado, y volver atras es redeployar el tag anterior. Un
# volumen compartido con un cron adentro deja la web sirviendo datos que
# nadie verifico, y sin forma de rollback.
#
# Refrescar el dato = reconstruir la imagen. El MAGYP publica una vez por mes.

# --------------------------------------------------------------------------
FROM python:3.12-slim AS datos

WORKDIR /build
RUN pip install --no-cache-dir pandas numpy requests pyarrow

COPY agroenso/ ./agroenso/
COPY etl/ ./etl/

# El clima previo, si el contexto de build lo trae. Su ETL tarda horas por la
# cuota de Open-Meteo, asi que NUNCA se reconstruye aca: se arrastra. Sin el,
# la web sirve igual el cruce ENSO x rinde, que es el nucleo del informe.
COPY data/snapshot/ /build/data/snapshot/

RUN python -m etl.construir --desde 1970 --salida /build/data/snapshot

# --------------------------------------------------------------------------
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    AGROENSO_SNAPSHOT=/app/data/snapshot \
    PORT=8000 \
    WEB_CONCURRENCY=2

WORKDIR /app

COPY requirements-web.txt .
RUN pip install --no-cache-dir -r requirements-web.txt

COPY agroenso/ ./agroenso/
COPY web/ ./web/
COPY --from=datos /build/data/snapshot/ ./data/snapshot/

# Import en seco de la app, con el snapshot ya adentro. Atrapa en el BUILD lo
# que si no aparece como un contenedor que arranca y muere: una dependencia
# del ETL que se colo en la cadena de imports de la web. Paso una vez de
# verdad — `analisis` importaba `clima`, que importaba `requests`, que esta
# imagen no instala.
RUN python -c "import web.api; print('import de la app OK')"

# Y la contracara: `requests` NO tiene que estar. Si alguien lo agrega a
# requirements-web.txt para 'arreglar' un import, esto lo frena y lo manda a
# arreglar la cadena, que es el problema real.
RUN python - <<'EOF'
import importlib.util, sys
if importlib.util.find_spec("requests") is not None:
    sys.exit("requests esta instalado en la imagen web: la capa de lectura "
             "no debe poder salir a la red. Revisar requirements-web.txt.")
print("sin cliente HTTP en la imagen web, como corresponde")
EOF

# Usuario sin privilegios: el proceso solo lee, nunca escribe en disco.
RUN useradd --create-home --uid 10001 agroenso && chown -R agroenso /app
USER agroenso

EXPOSE 8000

# El healthcheck pega al endpoint que confirma que el snapshot esta cargado,
# no a la raiz: la raiz devuelve el HTML aunque el motor no haya levantado.
HEALTHCHECK --interval=30s --timeout=5s --start-period=25s --retries=3 \
    CMD python -c "import urllib.request,os,sys; \
sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:'+os.environ['PORT']+'/api/salud', timeout=4).status==200 else 1)"

CMD ["sh", "-c", "exec uvicorn web.api:app --host 0.0.0.0 --port ${PORT} --workers ${WEB_CONCURRENCY}"]
