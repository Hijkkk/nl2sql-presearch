"""OpenAI-compatible local server for XiYanSQL-QwenCoder-3B-2504.

Start example:
    python scripts\serve_xiyan3b_openai.py --host 127.0.0.1 --port 8010

The MVP backend calls http://127.0.0.1:8010/v1/chat/completions when the
front end selects model_id=xiyan-sql-3b-finetune.
"""
from __future__ import annotations

import argparse
import os
import time
from typing import Any

import torch
import uvicorn
from fastapi import FastAPI
from pydantic import BaseModel, Field
from transformers import AutoModelForCausalLM, AutoTokenizer

DEFAULT_MODEL_PATH = r"Z:\python\Projects\task\datasources\XiYanSQL-QwenCoder-3b\XiYanSQL-QwenCoder-3B-2504"
MODEL_NAME = os.getenv("XIYAN_SQL_3B_MODEL", "XiYanSQL-QwenCoder-3B-2504")

app = FastAPI(title="XiYanSQL 3B OpenAI-compatible Server")
_tokenizer = None
_model = None


class ChatMessage(BaseModel):
    role: str
    content: str


class ChatCompletionRequest(BaseModel):
    model: str = MODEL_NAME
    messages: list[ChatMessage]
    temperature: float = 0.0
    top_p: float = 0.8
    max_tokens: int = Field(default=1024, alias="max_tokens")
    stream: bool = False


def load_model(model_path: str) -> None:
    global _tokenizer, _model
    if _model is not None:
        return
    _tokenizer = AutoTokenizer.from_pretrained(model_path, trust_remote_code=True)
    dtype = torch.bfloat16 if torch.cuda.is_available() else torch.float32
    _model = AutoModelForCausalLM.from_pretrained(
        model_path,
        torch_dtype=dtype,
        device_map="auto" if torch.cuda.is_available() else None,
        trust_remote_code=True,
    )
    _model.eval()


def render_prompt(messages: list[ChatMessage]) -> str:
    chat = [{"role": m.role, "content": m.content} for m in messages]
    if hasattr(_tokenizer, "apply_chat_template") and _tokenizer.chat_template:
        return _tokenizer.apply_chat_template(chat, tokenize=False, add_generation_prompt=True)
    return "\n".join(f"{m.role}: {m.content}" for m in messages) + "\nassistant:"


@app.get("/v1/models")
def list_models() -> dict[str, Any]:
    return {"object": "list", "data": [{"id": MODEL_NAME, "object": "model", "owned_by": "local"}]}


@app.post("/v1/chat/completions")
def chat_completions(payload: ChatCompletionRequest) -> dict[str, Any]:
    if payload.stream:
        raise ValueError("This lightweight local server only supports non-streaming responses.")

    prompt = render_prompt(payload.messages)
    inputs = _tokenizer(prompt, return_tensors="pt").to(_model.device)
    do_sample = payload.temperature > 0
    start = time.time()
    with torch.inference_mode():
        output_ids = _model.generate(
            **inputs,
            max_new_tokens=payload.max_tokens,
            do_sample=do_sample,
            temperature=payload.temperature if do_sample else None,
            top_p=payload.top_p if do_sample else None,
            pad_token_id=_tokenizer.eos_token_id,
        )
    generated_ids = output_ids[0][inputs["input_ids"].shape[-1]:]
    content = _tokenizer.decode(generated_ids, skip_special_tokens=True).strip()
    return {
        "id": f"chatcmpl-xiyan-{int(start * 1000)}",
        "object": "chat.completion",
        "created": int(start),
        "model": payload.model,
        "choices": [{"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}],
        "usage": {
            "prompt_tokens": int(inputs["input_ids"].shape[-1]),
            "completion_tokens": int(generated_ids.shape[-1]),
            "total_tokens": int(inputs["input_ids"].shape[-1] + generated_ids.shape[-1]),
        },
        "latency_seconds": round(time.time() - start, 3),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model-path", default=os.getenv("XIYAN_SQL_3B_MODEL_PATH", DEFAULT_MODEL_PATH))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8010)
    args = parser.parse_args()
    load_model(args.model_path)
    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
