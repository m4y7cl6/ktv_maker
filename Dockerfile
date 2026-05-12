FROM nvidia/cuda:12.1.1-cudnn8-runtime-ubuntu22.04

ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONUNBUFFERED=1
# 模型快取統一放到 /app/model_cache，再用 volume 掛載
ENV TORCH_HOME=/app/model_cache/torch
ENV HF_HOME=/app/model_cache/huggingface
ENV XDG_CACHE_HOME=/app/model_cache

# ── 系統套件 ──────────────────────────────────────────────
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3.11 python3-pip \
    ffmpeg \
    libass9 \
    fonts-noto-cjk \
    nodejs \
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

EXPOSE 8000

CMD ["uvicorn", "src.server:app", "--host", "0.0.0.0", "--port", "8000"]
