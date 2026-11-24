# Shopping list web app. Python standard library, plus `cryptography` for 6j0.org sign-in tokens (Ed25519).
FROM python:3.14.10-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    SHOPPING_HOST=0.0.0.0 \
    SHOPPING_PORT=8000 \
    SHOPPING_DB=/data/shopping.db

WORKDIR /app
COPY requirements.txt ./
RUN pip install --only-binary=:all: -r requirements.txt
COPY server.py sections.py recipes.py feedback.py idtoken.py ./
COPY static/ static/

# Unprivileged user; the SQLite database and feedback.jsonl live on a volume at /data.
RUN useradd --system --uid 10001 --home-dir /app app \
 && mkdir /data && chown app /data
USER 10001
VOLUME /data

EXPOSE 8000
CMD ["python3", "server.py"]
