# Agent Observatory: watcher (ingest + daily reports) and read-only LAN dashboard on :8787.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    OBS_TRANSCRIPTS=/transcripts OBS_DATA=/data OBS_INTERVAL=10 OBS_HOST=nas OBS_BIND=0.0.0.0 OBS_PORT=8787

WORKDIR /app
COPY pyproject.toml README.md ./
COPY obs ./obs
RUN pip install --no-cache-dir . \
 && useradd --uid 1000 --create-home --shell /usr/sbin/nologin observatory \
 && mkdir -p /data /transcripts && chown observatory:observatory /data

USER observatory
VOLUME ["/data"]
EXPOSE 8787
HEALTHCHECK --interval=60s --timeout=5s --start-period=30s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8787/api/health', timeout=4)" || exit 1
CMD ["obs", "watch"]
