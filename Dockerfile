FROM python:3.12-slim

# Standard library only: nothing to pip install.
WORKDIR /srv
COPY app ./app

# /data is writable by whichever user start.sh runs the container as.
RUN mkdir -p /data && chmod 1777 /data
USER 1000

ENV DATA_DIR=/data PORT=8765 PYTHONUNBUFFERED=1
EXPOSE 8765
VOLUME ["/data"]

HEALTHCHECK --interval=30s --timeout=5s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8765/api/status', timeout=4)"

CMD ["python", "-m", "app.server"]
