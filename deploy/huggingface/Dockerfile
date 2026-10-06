# Paste this as the only file in a Hugging Face Docker Space.
# It downloads the editor from GitHub and runs it.
FROM python:3.12-slim-trixie
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg fonts-dejavu-core git \
    && rm -rf /var/lib/apt/lists/*
ADD https://api.github.com/repos/lovedeep751653/ai-video-editor/git/refs/heads/main /tmp/version.json
RUN git clone --depth 1 https://github.com/lovedeep751653/ai-video-editor /srv \
    && pip install --no-cache-dir -r /srv/requirements.txt
ENV EDITOR_DATA=/tmp/editor-data
EXPOSE 7860
CMD ["sh", "-c", "cd /srv/app && uvicorn server:app --host 0.0.0.0 --port 7860"]
