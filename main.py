import os
import json
import httpx
import secrets
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import StreamingResponse, RedirectResponse
from fastapi.middleware.cors import CORSMiddleware
import asyncpg

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ===== ENV =====
CEREBRAS_API_KEY = os.environ.get("OPENROUTER_API_KEY", "") # نفس المتغير في Railway
APP_SECRET = os.environ.get("APP_SECRET", "darkai-2024-secret-key")
MODEL = os.environ.get("MODEL", "llama-3.3-70b")
SYSTEM_PROMPT = os.environ.get("SYSTEM_PROMPT", "أنت Dark AI، مساعد ذكاء اصطناعي عربي. أجب بوضوح ودقة.")
BASE_URL = os.environ.get("BASE_URL", "https://web-production-cd4a4.up.railway.app")
APP_DEEP_LINK = os.environ.get("APP_DEEP_LINK", "darkai://oauth")

GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "")
GITHUB_CLIENT_ID = os.environ.get("GITHUB_CLIENT_ID", "")
GITHUB_CLIENT_SECRET = os.environ.get("GITHUB_CLIENT_SECRET", "")

DATABASE_URL = os.environ.get("DATABASE_URL", "")

# ===== DB Pool =====
_pool = None

async def get_pool():
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=5)
    return _pool

@app.on_event("startup")
async def startup():
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id TEXT PRIMARY KEY, email TEXT, name TEXT, picture TEXT,
                provider TEXT, created_at TIMESTAMP DEFAULT NOW(), last_login TIMESTAMP DEFAULT NOW()
            );
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS sessions (
                token TEXT PRIMARY KEY, user_id TEXT REFERENCES users(id) ON DELETE CASCADE,
                created_at TIMESTAMP DEFAULT NOW()
            );
        """)

# ===== Helpers =====
async def verify_token(token: str):
    if not token: return None
    if token == APP_SECRET: return {"role": "admin"}
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow("SELECT user_id FROM sessions WHERE token = $1", token)
            if row: return {"role": "user", "user_id": row["user_id"]}
    except Exception: pass
    return None

async def upsert_user(user_id, email, name, picture, provider):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO users (id, email, name, picture, provider)
            VALUES ($1, $2, $3, $4, $5)
            ON CONFLICT (id) DO UPDATE SET email = EXCLUDED.email, name = EXCLUDED.name, last_login = NOW()
        """, user_id, email, name, picture, provider)

async def create_session(user_id):
    token = secrets.token_urlsafe(32)
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("INSERT INTO sessions (token, user_id) VALUES ($1, $2)", token, user_id)
    return token

# ===== Routes =====
@app.get("/")
def root():
    return {"status": "ok", "service": "Dark AI Proxy (Cerebras)"}

@app.post("/v1/chat/completions")
async def chat(request: Request):
    auth = request.headers.get("Authorization", "")
    token = auth.replace("Bearer ", "")

    user = await verify_token(token)
    if not user:
        raise HTTPException(status_code=401, detail="Unauthorized")

    body = await request.json()
    body["model"] = MODEL

    incoming = body.get("messages", [])
    filtered = [m for m in incoming if m.get("role") != "system"]
    body["messages"] = [{"role": "system", "content": SYSTEM_PROMPT}] + filtered
    body["stream"] = True
    
    # Cerebras ما يحتاجش إعدادات reasoning
    if "reasoning" in body:
        del body["reasoning"]

    headers = {
        "Authorization": f"Bearer {CEREBRAS_API_KEY}",
        "Content-Type": "application/json",
    }

    async def stream_response():
        async with httpx.AsyncClient(timeout=None) as client:
            async with client.stream(
                "POST",
                "https://api.cerebras.ai/v1/chat/completions",
                json=body,
                headers=headers,
            ) as resp:
                if resp.status_code != 200:
                    err_body = await resp.aread()
                    err_text = err_body.decode('utf-8', errors='ignore')[:500]
                    print(f"[CEREBRAS ERROR] {err_text}")
                    error_payload = json.dumps({"error": f"HTTP {resp.status_code} - {err_text}"})
                    yield f"data: {error_payload}\n\n".encode()
                    yield b"data: [DONE]\n\n"
                    return
                
                async for chunk in resp.aiter_bytes():
                    yield chunk

    return StreamingResponse(stream_response(), media_type="text/event-stream")

# ===== OAuth (كما هو بدون تغيير) =====
@app.get("/auth/google")
def auth_google():
    redirect_uri = f"{BASE_URL}/auth/callback/google"
    url = f"https://accounts.google.com/o/oauth2/v2/auth?client_id={GOOGLE_CLIENT_ID}&redirect_uri={redirect_uri}&response_type=code&scope=openid%20email%20profile"
    return RedirectResponse(url)

@app.get("/auth/callback/{provider}")
async def auth_callback(provider: str, code: str):
    if provider == "google": return await google_callback(code)
    raise HTTPException(400, "Unknown provider")

async def google_callback(code: str):
    redirect_uri = f"{BASE_URL}/auth/callback/google"
    async with httpx.AsyncClient() as client:
        resp = await client.post("https://oauth2.googleapis.com/token", data={
            "code": code, "client_id": GOOGLE_CLIENT_ID, "client_secret": GOOGLE_CLIENT_SECRET,
            "redirect_uri": redirect_uri, "grant_type": "authorization_code",
        })
        tokens = resp.json()
        access_token = tokens.get("access_token")
        if not access_token: return RedirectResponse(f"{APP_DEEP_LINK}?error=token_exchange_failed")
        
        user_resp = await client.get("https://www.googleapis.com/oauth2/v2/userinfo", headers={"Authorization": f"Bearer {access_token}"})
        user = user_resp.json()

    user_id = f"google:{user.get('id')}"
    await upsert_user(user_id, user.get("email"), user.get("name"), user.get("picture"), "google")
    session_token = await create_session(user_id)
    return RedirectResponse(f"{APP_DEEP_LINK}?token={session_token}")

@app.get("/auth/users")
async def list_users(request: Request):
    auth = request.headers.get("Authorization", "")
    if auth.replace("Bearer ", "") != APP_SECRET: raise HTTPException(403, "Admin only")
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT id, email, name, provider, created_at, last_login FROM users ORDER BY last_login DESC")
    return [dict(r) for r in rows]
