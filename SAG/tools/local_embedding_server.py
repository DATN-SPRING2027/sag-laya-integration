"""OpenAI-compatible local embedding server for SAG.

Run from ``SAG/apps/api`` with the API virtualenv so the locally installed
sentence-transformers runtime is used. The model is loaded once at startup
and served only on loopback by the recommended uvicorn command.
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import numpy as np
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
from sentence_transformers import SentenceTransformer


SAG_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MODEL_PATH = SAG_ROOT / "models" / "qwen3-embedding-0.6b"
MODEL_NAME = os.getenv("LOCAL_EMBEDDING_MODEL", "Qwen3-Embedding-0.6B")
MODEL_PATH = Path(os.getenv("LOCAL_EMBEDDING_MODEL_PATH", str(DEFAULT_MODEL_PATH)))
DEVICE = os.getenv("LOCAL_EMBEDDING_DEVICE", "cpu")
DIMENSION = 1024


class EmbeddingRequest(BaseModel):
    model: str = Field(min_length=1)
    input: str | list[str]
    dimensions: int | None = None


class EmbeddingItem(BaseModel):
    object: str = "embedding"
    embedding: list[float]
    index: int


class EmbeddingResponse(BaseModel):
    object: str = "list"
    data: list[EmbeddingItem]
    model: str
    usage: dict[str, int]


runtime: SentenceTransformer | None = None


@asynccontextmanager
async def lifespan(_app: FastAPI):
    global runtime
    if not MODEL_PATH.is_dir():
        raise RuntimeError(f"Local embedding model directory not found: {MODEL_PATH}")
    runtime = SentenceTransformer(str(MODEL_PATH), trust_remote_code=True, device=DEVICE)
    yield
    runtime = None


app = FastAPI(title="SAG Local Embedding Server", version="1.0.0", lifespan=lifespan)


@app.get("/health")
def health() -> dict[str, Any]:
    return {
        "status": "ok" if runtime is not None else "starting",
        "model": MODEL_NAME,
        "model_path": str(MODEL_PATH),
        "device": DEVICE,
        "dimension": DIMENSION,
    }


@app.get("/v1/models")
def models() -> dict[str, Any]:
    return {
        "object": "list",
        "data": [{"id": MODEL_NAME, "object": "model", "owned_by": "local"}],
    }


@app.post("/v1/embeddings", response_model=EmbeddingResponse)
def embeddings(request: EmbeddingRequest) -> EmbeddingResponse:
    if runtime is None:
        raise HTTPException(status_code=503, detail="Embedding model is not ready")
    if request.dimensions not in (None, DIMENSION):
        raise HTTPException(
            status_code=422,
            detail=f"Only dimensions={DIMENSION} is supported by {MODEL_NAME}",
        )

    inputs = [request.input] if isinstance(request.input, str) else request.input
    if not inputs or any(not item.strip() for item in inputs):
        raise HTTPException(status_code=422, detail="input must contain non-empty text")

    vectors = runtime.encode(
        inputs,
        normalize_embeddings=True,
        convert_to_numpy=True,
        show_progress_bar=False,
    )
    vectors = np.asarray(vectors, dtype=np.float32)
    if vectors.ndim != 2 or vectors.shape != (len(inputs), DIMENSION):
        raise HTTPException(status_code=500, detail="Embedding model returned an invalid shape")
    if not np.isfinite(vectors).all():
        raise HTTPException(status_code=500, detail="Embedding model returned NaN or Inf")

    return EmbeddingResponse(
        data=[
            EmbeddingItem(embedding=vector.tolist(), index=index)
            for index, vector in enumerate(vectors)
        ],
        model=request.model,
        usage={"prompt_tokens": 0, "total_tokens": 0},
    )
