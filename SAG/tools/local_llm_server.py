"""Minimal OpenAI-compatible local chat server for SAG extraction smoke tests.

This utility serves a locally downloaded causal instruct model on loopback. SAG
ingestion uses the OpenAI-compatible ``/v1/chat/completions`` contract through
LiteLLM; structured extraction is configured with ``prompt_only`` so SAG keeps
ownership of JSON parsing and schema validation.
"""

from __future__ import annotations

import os
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import torch
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
from transformers import AutoModelForCausalLM, AutoTokenizer


SAG_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MODEL_PATH = SAG_ROOT / "models" / "qwen2.5-1.5b-instruct"
MODEL_NAME = os.getenv("LOCAL_LLM_MODEL", "Qwen2.5-1.5B-Instruct")
MODEL_PATH = Path(os.getenv("LOCAL_LLM_MODEL_PATH", str(DEFAULT_MODEL_PATH)))
DEVICE = os.getenv("LOCAL_LLM_DEVICE", "cpu")
DEFAULT_MAX_NEW_TOKENS = int(os.getenv("LOCAL_LLM_MAX_NEW_TOKENS", "2048"))


class ChatMessage(BaseModel):
    role: str
    content: str | list[dict[str, Any]] | None = None


class ChatCompletionRequest(BaseModel):
    model: str = Field(min_length=1)
    messages: list[ChatMessage] = Field(min_length=1)
    temperature: float | None = None
    max_tokens: int | None = None
    max_completion_tokens: int | None = None
    stream: bool = False
    response_format: dict[str, Any] | None = None


tokenizer: AutoTokenizer | None = None
model: AutoModelForCausalLM | None = None


def _message_text(message: ChatMessage) -> dict[str, str]:
    content = message.content
    if isinstance(content, list):
        content = "\n".join(
            str(part.get("text", "")) for part in content if part.get("type") == "text"
        )
    return {"role": message.role, "content": content or ""}


def _add_structured_extraction_hint(
    messages: list[dict[str, str]], response_format: dict[str, Any] | None
) -> list[dict[str, str]]:
    """Give small local instruct models a compact JSON contract reminder.

    SAG still performs the authoritative JSON parsing, schema validation, and
    semantic validation. This hint only compensates for local models that do
    not implement OpenAI response_format enforcement themselves.
    """
    if not response_format or not messages:
        return messages
    hint = (
        "\n\nSTRICT OUTPUT REMINDER: Return only JSON for the SAG extraction contract. "
        "The root must be {\"type\":\"response\",\"data\":{\"items\":[...]}}; "
        "data is an object, never an array, and uses only {items}. Each item must use only "
        "{reason, title, summary, content, references, entities, is_valid, children}; "
        "never add id. references is mandatory and must be the integer array [1], never [] "
        "and never strings. "
        "Each valid item must include at least one entity with type, name, description; "
        "use an allowed entity type from meta.entity_types."
    )
    messages[-1]["content"] = f'{messages[-1]["content"]}{hint}'
    return messages


@asynccontextmanager
async def lifespan(_app: FastAPI):
    global model, tokenizer
    if not MODEL_PATH.is_dir():
        raise RuntimeError(f"Local LLM model directory not found: {MODEL_PATH}")
    tokenizer = AutoTokenizer.from_pretrained(str(MODEL_PATH), local_files_only=True)
    model = AutoModelForCausalLM.from_pretrained(
        str(MODEL_PATH),
        local_files_only=True,
        torch_dtype=torch.float32,
        low_cpu_mem_usage=True,
    )
    model.to(DEVICE)
    model.eval()
    yield
    model = None
    tokenizer = None


app = FastAPI(title="SAG Local LLM Server", version="1.0.0", lifespan=lifespan)


@app.get("/health")
def health() -> dict[str, Any]:
    return {
        "status": "ok" if model is not None and tokenizer is not None else "starting",
        "model": MODEL_NAME,
        "model_path": str(MODEL_PATH),
        "device": DEVICE,
    }


@app.get("/v1/models")
def models() -> dict[str, Any]:
    return {
        "object": "list",
        "data": [{"id": MODEL_NAME, "object": "model", "owned_by": "local"}],
    }


@app.post("/v1/chat/completions")
def chat_completions(request: ChatCompletionRequest) -> dict[str, Any]:
    if model is None or tokenizer is None:
        raise HTTPException(status_code=503, detail="Local LLM is not ready")
    if request.stream:
        raise HTTPException(status_code=400, detail="Streaming is not supported by this local utility")

    messages = [_message_text(message) for message in request.messages]
    messages = _add_structured_extraction_hint(messages, request.response_format)
    prompt = tokenizer.apply_chat_template(
        messages,
        tokenize=False,
        add_generation_prompt=True,
    )
    inputs = tokenizer(prompt, return_tensors="pt")
    inputs = {key: value.to(DEVICE) for key, value in inputs.items()}
    requested_tokens = request.max_completion_tokens or request.max_tokens or DEFAULT_MAX_NEW_TOKENS
    max_new_tokens = max(1, min(requested_tokens, DEFAULT_MAX_NEW_TOKENS))
    temperature = request.temperature if request.temperature is not None else 0.1
    do_sample = temperature > 0
    generation_kwargs: dict[str, Any] = {
        **inputs,
        "max_new_tokens": max_new_tokens,
        "do_sample": do_sample,
        "pad_token_id": tokenizer.eos_token_id,
    }
    if do_sample:
        generation_kwargs["temperature"] = temperature

    with torch.inference_mode():
        output = model.generate(**generation_kwargs)
    generated = output[0, inputs["input_ids"].shape[1] :]
    content = tokenizer.decode(generated, skip_special_tokens=True).strip()
    if not content:
        raise HTTPException(status_code=502, detail="Local LLM returned an empty response")

    prompt_tokens = int(inputs["input_ids"].shape[1])
    completion_tokens = int(generated.shape[0])
    return {
        "id": f"local-{uuid.uuid4().hex}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": request.model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        },
    }
