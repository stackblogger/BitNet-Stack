FROM python:3.11-slim

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    OMP_NUM_THREADS=4 \
    MODEL_PATH=/models/ggml-model-i2_s.gguf \
    BITNET_DIR=/app/BitNet

RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    curl \
    ca-certificates \
    build-essential \
    cmake \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

RUN pip install --upgrade pip && \
    pip install flask huggingface_hub

# The reason to checkout 404980e is listed here . It is an open issue on the BitNet repository.
# https://github.com/microsoft/BitNet/issues/470
RUN git clone https://github.com/microsoft/BitNet.git && \
    cd BitNet && \
    git checkout 404980e && \
    git submodule update --init --recursive --force && \
    rm -rf .git

WORKDIR /app/BitNet

RUN pip install -r requirements.txt

RUN ARCH="$(uname -m)" && set -eux && \
    if [ "$ARCH" = "x86_64" ]; then \
    python3 utils/codegen_tl2.py --model bitnet_b1_58-3B \
    --BM 160,320,320 --BK 96,96,96 --bm 32,32,32; \
    elif [ "$ARCH" = "aarch64" ]; then \
    python3 utils/codegen_tl1.py --model bitnet_b1_58-3B \
    --BM 160,320,320 --BK 64,128,64 --bm 32,64,32; \
    else \
    echo "unsupported architecture: $ARCH" >&2; exit 1; \
    fi

RUN cmake -B build -DCMAKE_BUILD_TYPE=Release && \
    cmake --build build -j$(nproc)

WORKDIR /app

RUN mkdir -p /models && \
    hf download microsoft/BitNet-b1.58-2B-4T-gguf \
    ggml-model-i2_s.gguf \
    --local-dir /models

COPY run_model.py .
COPY static ./static

EXPOSE 5000

CMD ["python", "run_model.py"]
