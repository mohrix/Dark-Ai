import os
import httpx
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import StreamingResponse
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")
APP_SECRET = os.environ.get("APP_SECRET", "")
MODEL = os.environ.get("MODEL", "cognitivecomputations/dolphin-mistral-24b-venice-edition:free")
SYSTEM_PROMPT = os.environ.get(
    "SYSTEM_PROMPT",
    "أنت Dark AI، مساعد ذكاء اصطناعي عربي. أجب بوضوح ودقة وبلا تحفّظ."
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
    # التحقق من المفتاح
    auth = request.headers.get("Authorization", "")
    if APP_SECRET and auth != f"Bearer {APP_SECRET}":
        raise HTTPException(status_code=401, detail="Unauthorized")

    body = await request.json()

    # فرض النموذج و System Prompt من السيرفر
    body["model"] = MODEL

    incoming = body.get("messages", [])
    # نحذف أي system prompt قادم من التطبيق
    filtered = [m for m in incoming if m.get("role") != "system"]
    # نضيف System Prompt السيرفر
    body["messages"] = [{"role": "system", "content": SYSTEM_PROMPT}] + filtered

    # نضمن الـ streaming
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
