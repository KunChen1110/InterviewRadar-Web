"""Optional OpenAI-compatible embeddings for semantic retrieval.

This is deliberately separate from chat-model settings: a chat-only DeepSeek key
must never be sent to an endpoint that does not support embeddings.
"""
from __future__ import annotations

import math
import os


def is_configured() -> bool:
    return bool(os.environ.get("EMBEDDING_API_KEY", "").strip() and os.environ.get("EMBEDDING_MODEL", "").strip())


def cosine_similarity(left: list[float] | None, right: list[float] | None) -> float | None:
    if not left or not right or len(left) != len(right):
        return None
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    if not left_norm or not right_norm:
        return None
    return sum(a * b for a, b in zip(left, right)) / (left_norm * right_norm)


def embed_texts(texts: list[str]) -> list[list[float]] | None:
    if not texts or not is_configured():
        return None
    try:
        from openai import OpenAI
        client = OpenAI(
            api_key=os.environ["EMBEDDING_API_KEY"],
            base_url=os.environ.get("EMBEDDING_BASE_URL", "https://api.openai.com/v1"),
            timeout=20.0,
            max_retries=1,
        )
        model = os.environ["EMBEDDING_MODEL"]
        dimensions = int(os.environ.get("EMBEDDING_DIMENSIONS", "1536"))
        vectors = []
        for start in range(0, len(texts), 10):
            response = client.embeddings.create(
                model=model,
                input=texts[start:start + 10],
                dimensions=dimensions,
            )
            vectors.extend(list(item.embedding) for item in response.data)
        return vectors if len(vectors) == len(texts) else None
    except Exception as exc:
        print(f"[embeddings] 调用失败（{type(exc).__name__}，回退关键词检索）")
        return None


def embed_one(text: str) -> list[float] | None:
    vectors = embed_texts([text])
    return vectors[0] if vectors else None
