from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
import os
import httpx

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")
APP_SECRET = os.environ.get("APP_SECRET", "darkai-secret")
MODEL = os.environ.get("MODEL", "meta-llama/llama-3.3-70b-instruct:free")
SYSTEM_PROMPT = os.environ.get(
    "SYSTEM_PROMPT",
    "أنت Dark AI، مساعد ذكاء اصطناعي عربي. أجب بوضوح ودقة."
)

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"


@app.get("/")
def root():
    return {"status": "ok", "service": "Dark AI Proxy"}


@app.get("/v1/models")
def models():
    return {"object": "list", "data": [{"id": MODEL, "object": "model"}]}


@app.post("/v1/chat/completions")
async def chat(request: Request):
    auth = request.headers.get("Authorization", "")
    if APP_SECRET and auth != f"Bearer {APP_SECRET}":
        raise HTTPException(status_code=401, detail="Unauthorized")

    body = await request.json()
    body["model"] = MODEL

    incoming = body.get("messages", [])
    filtered = [m for m in incoming if m.get("role") != "system"]
    body["messages"] = [{"role": "system", "content": SYSTEM_PROMPT}] + filtered
    body["stream"] = True

    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://dark-ai.app",
        "X-Title": "Dark AI",
    }

    async def stream():
        async with httpx.AsyncClient(timeout=None) as client:
            async with client.stream(
                "POST", OPENROUTER_URL, json=body, headers=headers
            ) as resp:
                async for chunk in resp.aiter_bytes():
                    yield chunk

    return StreamingResponse(stream(), media_type="text/event-stream")
