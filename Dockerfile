# Debian 13 ships FFmpeg 7, which has every transition the editor uses.
FROM python:3.12-slim-trixie
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
ENV EDITOR_DATA=/tmp/editor-data PORT=7860
EXPOSE 7860
CMD ["sh", "-c", "cd app && uvicorn server:app --host 0.0.0.0 --port ${PORT}"]
