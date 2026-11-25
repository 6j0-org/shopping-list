# The recipe generator (see server.py): Chef Transformer on CPU-only PyTorch. Build from this directory:
#   docker build -t recipegen recipegen/
# The model (about 900 MB) is downloaded while building, so the container starts without network access.
FROM python:3.14.10-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HF_HOME=/models \
    RECIPEGEN_HOST=0.0.0.0 \
    RECIPEGEN_PORT=8001

WORKDIR /app
COPY requirements.txt ./
RUN pip install --only-binary=:all: --extra-index-url https://download.pytorch.org/whl/cpu -r requirements.txt

# Pinned to a commit of the model repository, so a rebuild gets the same model. Only the files PyTorch needs.
ARG MODEL=flax-community/t5-recipe-generation
ARG MODEL_REVISION=12ba57f5c4924642afdf556a761124638e06af51
RUN python3 -c "from huggingface_hub import snapshot_download; snapshot_download('${MODEL}', revision='${MODEL_REVISION}', allow_patterns=['config.json', 'model.safetensors', 'tokenizer.json', 'tokenizer_config.json', 'special_tokens_map.json'])" \
 && chmod -R a+rX /models
ENV RECIPEGEN_MODEL=${MODEL} \
    RECIPEGEN_REVISION=${MODEL_REVISION} \
    HF_HUB_OFFLINE=1

COPY server.py ./

RUN useradd --system --uid 10001 --home-dir /app app
USER 10001

EXPOSE 8001
CMD ["python3", "server.py"]
