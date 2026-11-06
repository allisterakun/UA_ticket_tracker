FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY monitor.py .
RUN useradd -u 1000 -m app && mkdir -p /app/data && chown app /app/data
USER app

ENV CONFIG=/app/config.yaml STATE_FILE=/app/data/state.json
# Unhealthy if no cycle completed in the last 2 hours
HEALTHCHECK --interval=10m --timeout=10s \
  CMD python -c "import time,sys;sys.exit(time.time()-int(open('/app/data/heartbeat').read())>7200)"

ENTRYPOINT ["python", "monitor.py"]
