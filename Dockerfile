# cue_pipeline -- runs on Unraid alongside Lidarr, in the same path
# namespace (/downloads, /music) so there's no Windows<->container path
# translation and no SMB in the hot path.

# --- sacd_extract builder -------------------------------------------------
# Builds the sacd_extract CLI (sacd-ripper) that rips SACD ISOs to per-track
# DSF. Only the tiny binary is copied into the final image (needs libxml2 at
# runtime). Kept in a separate stage so its build toolchain never ships.
FROM debian:bookworm-slim AS sacd-builder
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        git cmake build-essential ca-certificates libxml2-dev \
    && git clone --depth 1 https://github.com/sacd-ripper/sacd-ripper.git /src \
    && cd /src/tools/sacd_extract \
    && cmake . \
    && make \
    && cp sacd_extract /sacd_extract

FROM python:3.11-slim

# ffmpeg = split/probe/DSD-decode;  tzdata = correct local timestamps in logs;
# libchromaprint-tools = fpcalc (acoustic fingerprinting for AcoustID identify);
# libdca-utils = dtsdec/dcadec, the libdca DTS decoder VLC uses -- needed for
#   14-bit DTS-CD streams that ffmpeg's built-in dca decoder can't frame;
# libxml2 = runtime dependency of the sacd_extract binary copied in below.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg tzdata libchromaprint-tools libdca-utils libxml2 \
    && rm -rf /var/lib/apt/lists/*

# SACD ISO ripper (built above). Present in PATH as `sacd_extract`.
COPY --from=sacd-builder /sacd_extract /usr/local/bin/sacd_extract

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
