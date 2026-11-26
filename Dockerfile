# syntax=docker/dockerfile:1
FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DATA_DIR=/data \
    HF_HOME=/models \
    PORT=8090

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

RUN useradd --uid 10001 --user-group --create-home --home-dir /home/app --shell /usr/sbin/nologin app \
    && mkdir -p /data /models \
    && chown 10001:10001 /data /models

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# deno: JavaScript runtime that current yt-dlp needs for YouTube. x86_64 only.
ARG DENO_VERSION=2.5.0
ADD https://github.com/denoland/deno/releases/download/v${DENO_VERSION}/deno-x86_64-unknown-linux-gnu.zip /tmp/deno.zip
RUN python -c "import zipfile; zipfile.ZipFile('/tmp/deno.zip').extract('deno', '/usr/local/bin')" \
    && chmod 0755 /usr/local/bin/deno \
    && rm /tmp/deno.zip \
    && deno --version

# yt-dlp in its own layer so bumping YTDLP_VERSION rebuilds only from here.
ARG YTDLP_VERSION=2026.11.23
RUN pip install --no-cache-dir "yt-dlp[default]==${YTDLP_VERSION}" \
    && yt-dlp --version

COPY app ./app

ARG IMAGE_SOURCE=https://github.com/loswastaken/whisper-transcriber
ARG GIT_REVISION=unknown
LABEL org.opencontainers.image.title="transcriber" \
      org.opencontainers.image.description="Self-hosted Whisper transcription web app (faster-whisper + yt-dlp)" \
      org.opencontainers.image.source="${IMAGE_SOURCE}" \
      org.opencontainers.image.revision="${GIT_REVISION}"

USER 10001:10001
EXPOSE 8090
VOLUME ["/data", "/models"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD ["python", "-c", "import sys, urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8090/healthz', timeout=4).status == 200 else 1)"]

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8090", "--workers", "1"]
