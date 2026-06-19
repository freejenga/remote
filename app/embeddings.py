"""Optional local embedding backend for semantic retrieval.

This module is the concrete implementation behind ``docstore``'s hybrid search.
It is deliberately *opt-in and local-only*, in keeping with the platform's
de-identification boundary: no text ever leaves the machine.

  * Disabled by default. Set ``PLATFORM_EMBEDDINGS=1`` (or true/yes/on) to enable.
  * When enabled, a small local sentence-transformer model is lazily loaded on
    first use (default ``BAAI/bge-small-en-v1.5``; override with
    ``PLATFORM_EMBED_MODEL``). The first load may fetch the model into the local
    HuggingFace cache once; thereafter it is fully offline.
  * Every entry point degrades gracefully: if embeddings are disabled, the model
    can't load (offline, not installed), or encoding fails, the functions return
    ``None`` and the caller falls back to TF-IDF. Nothing here ever raises.

Vectors are stored as little-ish float32 blobs via the stdlib ``array`` module,
so persistence has no hard NumPy dependency.
"""
import math
import os
import threading
from array import array
from typing import List, Optional, Sequence

_MODEL_NAME = os.environ.get("PLATFORM_EMBED_MODEL", "BAAI/bge-small-en-v1.5")

_model = None
_load_failed = False
_lock = threading.Lock()

_TRUTHY = {"1", "true", "yes", "on"}


def enabled() -> bool:
    """True if semantic embeddings are switched on via the environment."""
    return os.environ.get("PLATFORM_EMBEDDINGS", "").strip().lower() in _TRUTHY


def _get_model():
    """Lazily load (once) the local sentence-transformer model, or None."""
    global _model, _load_failed
    if _model is not None or _load_failed:
        return _model
    with _lock:
        if _model is None and not _load_failed:
            try:
                from sentence_transformers import SentenceTransformer
                _model = SentenceTransformer(_MODEL_NAME)
            except Exception:
                # Not installed, no cached model + offline, or load error:
                # remember the failure so we don't retry on every call.
                _load_failed = True
                _model = None
    return _model


def available() -> bool:
    """True only if embeddings are enabled *and* a model is usable right now."""
    return enabled() and _get_model() is not None


def embed(texts: Sequence[str]) -> Optional[List[List[float]]]:
    """Embed a batch of texts into unit-normalized vectors, or None on any miss.

    Returns ``None`` (never raises) when embeddings are disabled/unavailable so
    callers can fall back to lexical retrieval.
    """
    if not enabled():
        return None
    model = _get_model()
    if model is None:
        return None
    try:
        vecs = model.encode(list(texts), normalize_embeddings=True)
    except Exception:
        return None
    return [[float(x) for x in v] for v in vecs]


def embed_one(text: str) -> Optional[List[float]]:
    """Embed a single string, or None if unavailable."""
    out = embed([text])
    return out[0] if out else None


# --- vector (de)serialization + similarity (pure stdlib) -------------------
def to_blob(vec: Sequence[float]) -> bytes:
    """Pack a float vector into a compact float32 blob for storage."""
    return array("f", vec).tobytes()


def from_blob(blob: bytes) -> List[float]:
    """Unpack a float32 blob back into a list of floats."""
    a = array("f")
    a.frombytes(blob)
    return list(a)


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    """Cosine similarity between two vectors (robust to non-normalized input)."""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a)) or 1.0
    nb = math.sqrt(sum(y * y for y in b)) or 1.0
    return dot / (na * nb)
