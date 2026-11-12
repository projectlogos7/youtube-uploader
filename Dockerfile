FROM python:3.12-slim

# Standard library only: nothing to pip install.
WORKDIR /srv
COPY app ./app

RUN useradd --uid 1000 --create-home uploader \
 && mkdir -p /data /watch && chown uploader:uploader /data /watch
USER uploader

ENV WATCH_DIR=/watch DATA_DIR=/data PORT=8765 PUBLIC_URL=http://localhost:8765 PYTHONUNBUFFERED=1
EXPOSE 8765
VOLUME ["/data"]

HEALTHCHECK --interval=30s --timeout=5s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8765/api/status', timeout=4)"

CMD ["python", "-m", "app.server"]
