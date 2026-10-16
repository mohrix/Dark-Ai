import os
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
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")
APP_SECRET = os.environ.get("APP_SECRET", "darkai-2024-secret-key")
MODEL = os.environ.get("MODEL", "meta-llama/llama-3.3-70b-instruct:free")
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
                id TEXT PRIMARY KEY,
                email TEXT,
                name TEXT,
                picture TEXT,
                provider TEXT,
                created_at TIMESTAMP DEFAULT NOW(),
                last_login TIMESTAMP DEFAULT NOW()
            );
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS sessions (
                token TEXT PRIMARY KEY,
                user_id TEXT REFERENCES users(id) ON DELETE CASCADE,
                created_at TIMESTAMP DEFAULT NOW()
            );
        """)


# ===== Helpers =====
async def verify_token(token: str):
    if not token:
        return None
    if token == APP_SECRET:
        return {"role": "admin"}
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT user_id FROM sessions WHERE token = $1", token
            )
            if row:
                return {"role": "user", "user_id": row["user_id"]}
    except Exception:
        pass
    return None


async def upsert_user(user_id, email, name, picture, provider):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO users (id, email, name, picture, provider)
            VALUES ($1, $2, $3, $4, $5)
            ON CONFLICT (id) DO UPDATE SET
                email = EXCLUDED.email,
                name = EXCLUDED.name,
                picture = EXCLUDED.picture,
                last_login = NOW()
        """, user_id, email, name, picture, provider)


async def create_session(user_id):
    token = secrets.token_urlsafe(32)
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO sessions (token, user_id) VALUES ($1, $2)",
            token, user_id
        )
    return token


# ===== Routes =====
@app.get("/")
def root():
    return {"status": "ok", "service": "Dark AI Proxy"}


@app.get("/v1/models")
def models():
    return {"object": "list", "data": [{"id": MODEL, "object": "model"}]}


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

    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://dark-ai.app",
        "X-Title": "Dark AI",
    }

    async def stream():
    async with httpx.AsyncClient(timeout=None) as client:
        async with client.stream(
            "POST",
            "https://openrouter.ai/api/v1/chat/completions",
            json=body,
            headers=headers,
        ) as resp:
            print(f"[OPENROUTER] status={resp.status_code}")
            if resp.status_code != 200:
                error_body = await resp.aread()
                print(f"[OPENROUTER ERROR] {error_body.decode('utf-8', errors='ignore')[:500]}")
                yield f"data: {{\"error\": \"HTTP {resp.status_code}\"}}\n\n".encode()
                yield b"data: [DONE]\n\n"
                return
            async for chunk in resp.aiter_bytes():
                yield chunk

    return StreamingResponse(stream(), media_type="text/event-stream")


# ===== OAuth =====
@app.get("/auth/google")
def auth_google():
    if not GOOGLE_CLIENT_ID:
        raise HTTPException(500, "Google OAuth not configured")
    redirect_uri = f"{BASE_URL}/auth/callback/google"
    url = (
        "https://accounts.google.com/o/oauth2/v2/auth"
        f"?client_id={GOOGLE_CLIENT_ID}"
        f"&redirect_uri={redirect_uri}"
        "&response_type=code"
        "&scope=openid%20email%20profile"
        "&access_type=offline"
    )
    return RedirectResponse(url)


@app.get("/auth/github")
def auth_github():
    if not GITHUB_CLIENT_ID:
        raise HTTPException(500, "GitHub OAuth not configured")
    redirect_uri = f"{BASE_URL}/auth/callback/github"
    url = (
        "https://github.com/login/oauth/authorize"
        f"?client_id={GITHUB_CLIENT_ID}"
        f"&redirect_uri={redirect_uri}"
        "&scope=read:user%20user:email"
    )
    return RedirectResponse(url)


@app.get("/auth/callback/{provider}")
async def auth_callback(provider: str, code: str):
    if provider == "google":
        return await google_callback(code)
    if provider == "github":
        return await github_callback(code)
    raise HTTPException(400, "Unknown provider")


async def google_callback(code: str):
    redirect_uri = f"{BASE_URL}/auth/callback/google"
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            "https://oauth2.googleapis.com/token",
            data={
                "code": code,
                "client_id": GOOGLE_CLIENT_ID,
                "client_secret": GOOGLE_CLIENT_SECRET,
                "redirect_uri": redirect_uri,
                "grant_type": "authorization_code",
            },
        )
        tokens = resp.json()
        access_token = tokens.get("access_token")
        if not access_token:
            return RedirectResponse(f"{APP_DEEP_LINK}?error=token_exchange_failed")

        user_resp = await client.get(
            "https://www.googleapis.com/oauth2/v2/userinfo",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        user = user_resp.json()

    user_id = f"google:{user.get('id')}"
    email = user.get("email")
    name = user.get("name")
    picture = user.get("picture")

    await upsert_user(user_id, email, name, picture, "google")
    session_token = await create_session(user_id)

    return RedirectResponse(f"{APP_DEEP_LINK}?token={session_token}")


async def github_callback(code: str):
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            "https://github.com/login/oauth/access_token",
            data={
                "client_id": GITHUB_CLIENT_ID,
                "client_secret": GITHUB_CLIENT_SECRET,
                "code": code,
            },
            headers={"Accept": "application/json"},
        )
        tokens = resp.json()
        access_token = tokens.get("access_token")
        if not access_token:
            return RedirectResponse(f"{APP_DEEP_LINK}?error=token_exchange_failed")

        user_resp = await client.get(
            "https://api.github.com/user",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        user = user_resp.json()

        email = user.get("email")
        if not email:
            emails_resp = await client.get(
                "https://api.github.com/user/emails",
                headers={"Authorization": f"Bearer {access_token}"},
            )
            emails = emails_resp.json()
            if isinstance(emails, list):
                primary = next((e for e in emails if e.get("primary")), None)
                email = primary.get("email") if primary else None

    user_id = f"github:{user.get('id')}"
    name = user.get("name") or user.get("login")
    picture = user.get("avatar_url")

    await upsert_user(user_id, email, name, picture, "github")
    session_token = await create_session(user_id)

    return RedirectResponse(f"{APP_DEEP_LINK}?token={session_token}")


@app.get("/auth/me")
async def auth_me(request: Request):
    auth = request.headers.get("Authorization", "")
    token = auth.replace("Bearer ", "")

    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("""
            SELECT u.id, u.email, u.name, u.picture, u.provider,
                   u.created_at, u.last_login
            FROM users u
            JOIN sessions s ON s.user_id = u.id
            WHERE s.token = $1
        """, token)

    if not row:
        raise HTTPException(401, "Invalid session")

    return {
        "id": row["id"],
        "email": row["email"],
        "name": row["name"],
        "picture": row["picture"],
        "provider": row["provider"],
        "created_at": str(row["created_at"]),
        "last_login": str(row["last_login"]),
    }


@app.get("/auth/users")
async def list_users(request: Request):
    auth = request.headers.get("Authorization", "")
    token = auth.replace("Bearer ", "")
    if token != APP_SECRET:
        raise HTTPException(403, "Admin only")

    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT id, email, name, provider, created_at, last_login
            FROM users
            ORDER BY last_login DESC
        """)
    return [dict(r) for r in rows]
