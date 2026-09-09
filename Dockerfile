# The Hub — container image
#
# Bundles ffmpeg and yt-dlp so there's nothing to install on the host.
# Build:  docker build -t thehub .
# Run:    docker compose up -d

FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

# ffmpeg does thumbnails and format conversion; the rest is for building wheels.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
        curl \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Data lives on a volume so the database survives image rebuilds.
RUN mkdir -p /data && chmod 755 /data
ENV DB_PATH=/data/hub.db \
    UPLOAD_DIR=/data/uploads \
    CACHE_DIR=/data/cache \
    HLS_DIR=/data/hls \
    HOST=0.0.0.0 \
    PORT=5002

# Run as a non-root user. Media is mounted read-only, so this only needs to
# write to /data.
RUN useradd --create-home --uid 1000 hub && chown -R hub:hub /app /data
USER hub

EXPOSE 5002

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD curl -fsS http://localhost:5002/api/health || exit 1

CMD ["gunicorn", "-c", "gunicorn.conf.py", "wsgi:app"]
