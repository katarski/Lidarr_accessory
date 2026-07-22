# cue_pipeline -- runs on Unraid alongside Lidarr, in the same path
# namespace (/downloads, /music) so there's no Windows<->container path
# translation and no SMB in the hot path.
FROM python:3.11-slim

# ffmpeg = split/probe;  tzdata = correct local timestamps in logs
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Deps first for layer caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# App code only (no config -- that lives in the mounted /config volume)
COPY *.py /app/

ENV PYTHONUNBUFFERED=1 \
    TZ=Europe/Copenhagen

# /config  -> appdata (this holds config.yaml, logs, ledger, audit csv+sig)
# /downloads and /music are bind-mounted at run time, same as Lidarr.
VOLUME ["/config", "/downloads", "/music"]

# Config is read from the mounted volume so you can edit it without rebuilding.
ENTRYPOINT ["python", "main.py", "--config", "/config/config.yaml"]
