FROM python:3.11-slim
# Chromium is installed under a shared path so it stays readable after the switch to the non-root user.
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 WH_HOST=0.0.0.0 WH_DATA_DIR=/data PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt && python -m playwright install --with-deps chromium
COPY wormhole/ wormhole/
COPY web/ web/
COPY run.py .
# Run as an unprivileged user: the screen renders pages influenced by strangers, with --no-sandbox.
RUN useradd --create-home --shell /usr/sbin/nologin worm \
    && mkdir -p /data \
    && chown -R worm:worm /app /data /ms-playwright
USER worm
# The database lives in /data. Railway (and any other host) needs a persistent volume mounted at /data,
# otherwise every redeploy or restart starts from an empty database.
VOLUME ["/data"]
CMD ["python", "run.py"]
