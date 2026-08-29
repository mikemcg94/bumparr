FROM python:3.12-slim

# ffmpeg = ffprobe (durations) + live-cam snippet capture.
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Bumparr is self-contained: just the bumparr package.
COPY bumparr ./bumparr

EXPOSE 8780
CMD ["uvicorn", "bumparr.app:app", "--host", "0.0.0.0", "--port", "8780"]
