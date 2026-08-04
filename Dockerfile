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

# --- libdvd-audio builder --------------------------------------------------
# Builds dvda2wav (libdvd-audio), which decodes a DVD-Audio disc's AUDIO_TS
# (MLP/PCM .AOB files) to per-track WAV -- the DVD-Audio counterpart to
# sacd_extract. Self-contained (bundles its own MLP decoder + mini-gmp), so it
# only needs a C toolchain and m4 (for the pkg-config metadata). Only the tiny
# statically-linked binary ships in the final image.
FROM debian:bookworm-slim AS dvda-builder
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        git build-essential ca-certificates m4 \
    && git clone --depth 1 https://github.com/tuffy/libdvd-audio.git /src \
    && cd /src \
    && make dvda2wav \
    && cp dvda2wav /dvda2wav

FROM python:3.11-slim

# ffmpeg = split/probe/DSD-decode;  tzdata = correct local timestamps in logs;
# libchromaprint-tools = fpcalc (acoustic fingerprinting for AcoustID identify);
# libdca-utils = dtsdec/dcadec, the libdca DTS decoder VLC uses -- needed for
#   14-bit DTS-CD streams that ffmpeg's built-in dca decoder can't frame;
# libxml2 = runtime dependency of the sacd_extract binary copied in below;
# p7zip-full = `7z`, used to unpack an AUDIO_TS out of a DVD-Audio UDF ISO
#   without a loop-mount (the container has no loop devices / privileges).
# unar = The Unarchiver (`unar`/`lsar`), a full RAR decoder used as a FALLBACK
#   when 7z can't decode a RAR (p7zip lacks some RAR compression methods and
#   silently writes 0-byte files -- see _extract_archives_folder).
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg tzdata libchromaprint-tools libdca-utils libxml2 p7zip-full unar \
    && rm -rf /var/lib/apt/lists/*

# SACD ISO ripper (built above). Present in PATH as `sacd_extract`.
COPY --from=sacd-builder /sacd_extract /usr/local/bin/sacd_extract
# DVD-Audio track decoder (built above). Present in PATH as `dvda2wav`.
COPY --from=dvda-builder /dvda2wav /usr/local/bin/dvda2wav

WORKDIR /app

# Deps first for layer caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# App code only (no config -- that lives in the mounted /config volume)
COPY *.py /app/
COPY tools/ /app/tools/

ENV PYTHONUNBUFFERED=1 \
    TZ=Europe/Copenhagen

# Manual-attention WebUI (backlog #11). Publish with -p 8830:8830 to reach it.
EXPOSE 8830

# /config  -> appdata (this holds config.yaml, logs, ledger, audit csv+sig)
# /downloads and /music are bind-mounted at run time, same as Lidarr.
VOLUME ["/config", "/downloads", "/music"]

# Config is read from the mounted volume so you can edit it without rebuilding.
ENTRYPOINT ["python", "main.py", "--config", "/config/config.yaml"]
