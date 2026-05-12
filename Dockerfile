FROM nvidia/cuda:12.1.1-cudnn8-runtime-ubuntu22.04

ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONUNBUFFERED=1

# ── 系統套件 ──────────────────────────────────────────────
RUN apt-get update && apt-get install -y \
    python3.11 python3-pip python3.11-dev \
    ffmpeg \
    libass-dev \
    fonts-noto-cjk \
    nodejs \
    git curl \
    && rm -rf /var/lib/apt/lists/*

RUN ln -sf /usr/bin/python3.11 /usr/bin/python && \
    ln -sf /usr/bin/pip3 /usr/bin/pip

WORKDIR /app

# ── Python 套件 ───────────────────────────────────────────
COPY requirements.txt .

# PyTorch CUDA 12.1
RUN pip install --no-cache-dir \
    torch torchaudio --index-url https://download.pytorch.org/whl/cu121

RUN pip install --no-cache-dir -r requirements.txt

# ── 應用程式 ──────────────────────────────────────────────
COPY src/ ./src/

# 預下載 Demucs htdemucs 模型（避免首次執行時等待）
RUN python -c "import demucs.pretrained; demucs.pretrained.get_model('htdemucs')" || true

# 預下載 Whisper large-v3 模型
RUN python -c "from faster_whisper import WhisperModel; WhisperModel('large-v3', device='cpu')" || true

EXPOSE 8000

CMD ["uvicorn", "src.server:app", "--host", "0.0.0.0", "--port", "8000"]
