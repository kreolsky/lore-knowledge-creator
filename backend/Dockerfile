FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg libjpeg62-turbo zlib1g libwebp7 && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
# WHY: the CI runner intermittently times out reaching pypi.org with
# pip's default 15s timeout / 5 retries, failing the build with a misleading
# "No matching distribution" error. Longer timeout + more retries ride out the blips.
RUN pip install --no-cache-dir --retries 10 --timeout 60 -r requirements.txt
COPY . .
# Pinned artifact libraries (SYSTEM: artifact-serve): fetch + sha256-verify
# into /opt/artifact_lib — OUTSIDE /app, which dev/gray bind-mount over. A
# mismatch fails the build (backend/artifact_lib/fetch.py).
RUN python artifact_lib/fetch.py
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8001", "--reload"]
