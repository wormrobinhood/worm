FROM python:3.11-slim
# Chromium runs in Dockerfile.screen, outside the service that can hold signing keys.
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 WH_HOST=0.0.0.0 WH_DATA_DIR=/data
WORKDIR /app
COPY requirements-build.lock requirements.lock ./
RUN python -m pip install --require-hashes -r requirements-build.lock \
    && python -m pip install --require-hashes -r requirements.lock
COPY wormhole/ wormhole/
COPY web/ web/
COPY run.py .
# Run the signer as an unprivileged user. Live browser work uses a separate CDP service.
COPY docker-entrypoint.sh .
RUN useradd --uid 1000 --create-home --shell /usr/sbin/nologin worm \
    && mkdir -p /data \
    && chown -R worm:worm /data \
    && chmod +x /app/docker-entrypoint.sh
# The database lives in /data. Mount a persistent volume there (on Railway: a Railway Volume attached to the
# service at /data; Railway rejects a Docker VOLUME instruction), otherwise every redeploy starts from an
# empty database. Volumes arrive root-owned, so the entrypoint fixes ownership and then runs as "worm".
CMD ["/app/docker-entrypoint.sh"]
