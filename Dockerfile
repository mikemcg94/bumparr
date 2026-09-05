FROM python:3.12-slim@sha256:78387bc3881b8273120a12ebe6c1ab22b018ccc2c9adf565ae1ac9b536e184ea

# ffmpeg = ffprobe (durations) + live-cam snippet capture. Unpinned on purpose:
# Debian slim package revisions vanish as the repo advances, so an exact pin
# rots the build; the base image digest above pins the OS snapshot. To pin,
# build with e.g. --build-arg FFMPEG_VERSION=7:7.1.1-1+b1.
# apt security updates intentionally float within the pinned base's Debian suite.
ARG FFMPEG_VERSION=""
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg${FFMPEG_VERSION:+=$FFMPEG_VERSION} \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Bumparr is self-contained: just the bumparr package.
COPY bumparr ./bumparr

# Run as non-root. The default named volumes inherit ownership from these image
# directories. Operator-supplied bind mounts must be writable by this UID.
RUN groupadd -g 10001 appuser \
    && useradd -M -u 10001 -g 10001 -s /usr/sbin/nologin appuser \
    && mkdir -p /assets /data \
    && chown -R appuser:appuser /app /assets /data
USER appuser

EXPOSE 8780
HEALTHCHECK --interval=30s --timeout=10s --retries=3 CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8780/healthz', timeout=5).status==200 else 1)"
CMD ["uvicorn", "bumparr.app:app", "--host", "0.0.0.0", "--port", "8780"]
