FROM nvidia/cuda:12.4.1-cudnn-runtime-ubuntu22.04

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    python3 \
    python3-pip \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN python3 -m pip install --no-cache-dir -e ".[server,gpu]"

ENV AILIVE_BACKEND=mock \
    AILIVE_DATA_DIR=/data \
    AILIVE_MODEL_PATH=/models/Qwen3-TTS-12Hz-1.7B-Base

VOLUME ["/data", "/models"]
EXPOSE 8000
CMD ["python3", "-m", "uvicorn", "ailive.server.app:app", "--host", "0.0.0.0", "--port", "8000"]
