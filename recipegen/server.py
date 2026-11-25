"""The recipe generator: a small HTTP service around Chef Transformer, a T5 model trained on RecipeNLG
(https://huggingface.co/flax-community/t5-recipe-generation), that writes recipes from a list of ingredients.

It runs in its own container, next to the shopping list, because PyTorch is about 1 GB. Only the shopping list
should be able to reach it; it has no sign-in of its own.

    POST /generate  {"ingredients": ["chicken", "rice"], "count": 3}
        -> {"recipes": [{"title": str, "ingredients": [str], "directions": [str]}]}
        503 when it's already busy with as many requests as it queues.
    GET /healthz    -> {"ok": true} once the model is loaded.

The ingredient lines come back as the model writes them ("1 c. rice"); the shopping list splits them.
"""

import json
import os
import re
import sys
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MODEL = os.environ.get("RECIPEGEN_MODEL", "flax-community/t5-recipe-generation")
# The model repository's commit. Offline (as in the image), it must be the one that was downloaded: the cache only knows
# that commit, not which commit "main" is.
REVISION = os.environ.get("RECIPEGEN_REVISION", "12ba57f5c4924642afdf556a761124638e06af51")
MAX_INGREDIENTS = 15
MAX_COUNT = 3
QUEUE = int(os.environ.get("RECIPEGEN_QUEUE", "2"))  # requests that may wait while one is generating
WAIT_SECONDS = 60  # how long a queued request waits before getting a 503

# The model card's settings: sampling, so each recipe (and each try) differs.
GENERATION = {
    "max_length": 512,
    "min_length": 64,
    "no_repeat_ngram_size": 3,
    "do_sample": True,
    "top_k": 60,
    "top_p": 0.95,
}

_tokenizer = _model = None
_running = threading.Lock()  # the model generates one request at a time
_queue = threading.BoundedSemaphore(QUEUE + 1)


def load():
    global _tokenizer, _model
    import torch
    from transformers import AutoModelForSeq2SeqLM, AutoTokenizer

    torch.set_num_threads(int(os.environ.get("RECIPEGEN_THREADS", os.cpu_count() or 1)))
    _tokenizer = AutoTokenizer.from_pretrained(MODEL, revision=REVISION)
    _model = AutoModelForSeq2SeqLM.from_pretrained(MODEL, revision=REVISION)
    _model.eval()


def parse(text):
    """The model writes "title: … <section> ingredients: a <sep> b <section> directions: x <sep> y". Returns
    {title, ingredients: [str], directions: [str]}."""
    for token in ("<pad>", "</s>", "<s>"):
        text = text.replace(token, "")
    recipe = {"title": "", "ingredients": [], "directions": []}
    for section in text.split("<section>"):
        key, _, value = section.partition(":")
        key = key.strip().lower()
        if key == "title":
            recipe["title"] = value.strip()
        elif key in ("ingredients", "directions"):
            recipe[key] = [p.strip() for p in value.split("<sep>") if p.strip()]
    return recipe


def generate(ingredients, count):
    import torch

    prompt = "items: " + ", ".join(ingredients)
    inputs = _tokenizer([prompt], max_length=256, padding="max_length", truncation=True, return_tensors="pt")
    with torch.inference_mode():
        output = _model.generate(
            input_ids=inputs.input_ids, attention_mask=inputs.attention_mask,
            num_return_sequences=count, **GENERATION,
        )
    # The section markers are added tokens, not special ones, so they survive decoding without skipping specials.
    texts = _tokenizer.batch_decode(output, skip_special_tokens=False)
    return [parse(t) for t in texts]


def _clean_request(data):
    if not isinstance(data, dict):
        raise ValueError("expected a JSON object")
    names = data.get("ingredients")
    if not isinstance(names, list) or not names or len(names) > MAX_INGREDIENTS:
        raise ValueError(f"ingredients must be a list of 1 to {MAX_INGREDIENTS} names")
    clean = []
    for n in names:
        if not isinstance(n, str):
            raise ValueError("each ingredient must be a string")
        n = re.sub(r"\s+", " ", n).strip()[:100]
        if n:
            clean.append(n)
    if not clean:
        raise ValueError("ingredients must not be blank")
    count = data.get("count", MAX_COUNT)
    if not isinstance(count, int) or not 1 <= count <= MAX_COUNT:
        raise ValueError(f"count must be 1 to {MAX_COUNT}")
    return clean, count


class Handler(BaseHTTPRequestHandler):
    def _send(self, status, payload):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/healthz":
            return self._send(HTTPStatus.OK, {"ok": _model is not None})
        self._send(HTTPStatus.NOT_FOUND, {"error": "not found"})

    def do_POST(self):
        if self.path != "/generate":
            return self._send(HTTPStatus.NOT_FOUND, {"error": "not found"})
        try:
            length = int(self.headers.get("Content-Length") or 0)
            if length > 16_000:
                raise ValueError("request too large")
            ingredients, count = _clean_request(json.loads(self.rfile.read(length) or b"{}"))
        except ValueError as e:  # includes bad JSON
            return self._send(HTTPStatus.BAD_REQUEST, {"error": str(e)})
        if not _queue.acquire(blocking=False):
            return self._send(HTTPStatus.SERVICE_UNAVAILABLE, {"error": "busy"})
        try:
            if not _running.acquire(timeout=WAIT_SECONDS):
                return self._send(HTTPStatus.SERVICE_UNAVAILABLE, {"error": "busy"})
            try:
                started = time.monotonic()
                recipes = generate(ingredients, count)
                print(f"generated {count} from {len(ingredients)} ingredients in {time.monotonic() - started:.1f}s",
                      flush=True)
            finally:
                _running.release()
        except Exception as e:
            print(f"generation failed: {e!r}", file=sys.stderr, flush=True)
            return self._send(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "generation failed"})
        finally:
            _queue.release()
        self._send(HTTPStatus.OK, {"recipes": recipes})

    def log_message(self, fmt, *args):
        if self.path != "/healthz":
            super().log_message(fmt, *args)


def main():
    host = os.environ.get("RECIPEGEN_HOST", "127.0.0.1")
    port = int(os.environ.get("RECIPEGEN_PORT", "8001"))
    print(f"loading {MODEL} at {REVISION[:12]}…", flush=True)
    load()
    print(f"recipe generator on http://{host}:{port}", flush=True)
    ThreadingHTTPServer((host, port), Handler).serve_forever()


if __name__ == "__main__":
    main()
