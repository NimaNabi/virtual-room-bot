FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 DATA_DIR=/data CONFIG_PATH=/data/server.yaml
WORKDIR /app

COPY requirements.txt requirements-dev.txt ./
RUN pip install -r requirements.txt -r requirements-dev.txt

COPY vrbot ./vrbot
COPY tests ./tests
COPY pytest.ini VERSION ./
COPY config ./config

RUN useradd --uid 1000 --create-home vrbot && mkdir -p /data && chown vrbot /data
USER vrbot

HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 CMD ["python", "-m", "vrbot.cli", "health"]
CMD ["python", "-m", "vrbot"]
