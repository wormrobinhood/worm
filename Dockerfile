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
COPY docker-entrypoint.sh .
RUN useradd --uid 1000 --create-home --shell /usr/sbin/nologin worm \
    && mkdir -p /data \
    && chown -R worm:worm /app /data /ms-playwright \
    && chmod +x /app/docker-entrypoint.sh
# The database lives in /data. Mount a persistent volume there (on Railway: a Railway Volume attached to the
# service at /data; Railway rejects a Docker VOLUME instruction), otherwise every redeploy starts from an
# empty database. Volumes arrive root-owned, so the entrypoint fixes ownership and then runs as "worm".
CMD ["/app/docker-entrypoint.sh"]
