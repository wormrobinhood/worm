FROM python:3.11-slim
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 WH_HOST=0.0.0.0 WH_DATA_DIR=/data
WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt && python -m playwright install --with-deps chromium
COPY wormhole/ wormhole/
COPY web/ web/
COPY run.py .
CMD ["python", "run.py"]
