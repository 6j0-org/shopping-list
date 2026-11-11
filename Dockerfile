# Shopping list web app. Python standard library only, so no pip install step.
FROM python:3.14.10-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    SHOPPING_HOST=0.0.0.0 \
    SHOPPING_PORT=8000 \
    SHOPPING_DB=/data/shopping.db

WORKDIR /app
COPY server.py sections.py recipes.py ./
COPY static/ static/

# Unprivileged user; the SQLite database lives on a volume at /data.
RUN useradd --system --uid 10001 --home-dir /app app \
 && mkdir /data && chown app /data
USER 10001
VOLUME /data

EXPOSE 8000
CMD ["python3", "server.py"]
