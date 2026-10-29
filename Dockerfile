FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /srv

# tzdata, damit TZ=Europe/Berlin wirkt (ist im slim-Image nicht mehr enthalten)
RUN apt-get update \
 && apt-get install -y --no-install-recommends tzdata \
 && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install -r requirements.txt

# Eigener Benutzer ohne Root-Rechte; /data gehört ihm, damit das Volume beschreibbar ist.
RUN useradd --system --uid 10001 --home-dir /srv --shell /usr/sbin/nologin dashboard \
 && mkdir -p /data \
 && chown dashboard:dashboard /data

COPY app ./app

USER dashboard
EXPOSE 8080
VOLUME ["/data"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD ["python", "-c", "import sys, urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=4).status == 200 else 1)"]

# Genau ein Worker: Sitzungs-Bremse und Restart-Verlauf liegen im Speicher.
CMD ["uvicorn", "app.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8080", \
     "--workers", "1", "--no-server-header", "--no-access-log"]
