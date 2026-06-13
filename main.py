"""
Junior.so → OpenAI-compatible Proxy Server
===========================================

Converts junior.so's proprietary chat API into OpenAI-compatible
/v1/chat/completions format so it can be used with 9router or any
OpenAI-compatible client.

Multi-account support with round-robin rotation.

Auth: Pass junior.so credentials via the Authorization header:
  Authorization: Bearer <token>|<uid>|<junior_id>

Or set environment variables (comma-separated for multiple accounts):
  JUNIOR_ACCOUNTS=token1|uid1|jid1,token2|uid2|jid2,...
  (Legacy single-account: JUNIOR_TOKEN, JUNIOR_UID, JUNIOR_ID)

Manage accounts at runtime (requires ADMIN_KEY):
  GET  /accounts        - List all accounts
  POST /accounts        - Add account {"token":..., "uid":..., "junior_id":...}
  DELETE /accounts/{id} - Remove account by index

Usage:
  uvicorn main:app --host 0.0.0.0 --port 8000

Then in 9router or any OpenAI client:
  base_url = "http://<server>:8000/v1"
  api_key  = "any"  (uses round-robin from pool)
  model    = "junior"
"""

import json
import time
import uuid
import os
import threading
from typing import AsyncIterator

import httpx
from fastapi import FastAPI, Request, Depends
from fastapi.responses import StreamingResponse, JSONResponse

from fastapi.responses import FileResponse as _FileResponse
from pathlib import Path

app = FastAPI(title="Junior.so OpenAI Proxy", version="0.3.0")

_DIR = Path(__file__).parent


@app.get("/test")
async def test_page():
    return _FileResponse(_DIR / "test.html")

JUNIOR_BASE = "https://junior.so/api"
ACCOUNTS_FILE = _DIR / "accounts.json"
ADMIN_KEY = os.environ.get("ADMIN_KEY", "")


# ── Admin auth dependency ───────────────────────────────────
async def require_admin(request: Request):
    """Verify admin key for account management endpoints."""
    if not ADMIN_KEY:
        return  # No key configured = no protection (local dev)
    auth = request.headers.get("authorization", "")
    key = request.query_params.get("admin_key", "")
    if auth == f"Bearer {ADMIN_KEY}" or key == ADMIN_KEY:
        return
    return JSONResponse(
        status_code=403,
        content={"error": "Forbidden. Set ADMIN_KEY env var and pass it via Authorization: Bearer <key> or ?admin_key=<key>"},
    )


# ── Account Pool ────────────────────────────────────────────
class AccountPool:
    """Thread-safe round-robin pool of junior.so accounts."""

    def __init__(self):
        self._accounts: list[dict] = []
        self._lock = threading.Lock()
        self._index = 0

    def load_from_env(self):
        """Load accounts from environment variables."""
        accounts_str = os.environ.get("JUNIOR_ACCOUNTS", "")
        if accounts_str:
            for entry in accounts_str.split(","):
                parts = entry.strip().split("|")
                if len(parts) == 3:
                    self.add(parts[0], parts[1], parts[2])

        token = os.environ.get("JUNIOR_TOKEN", "")
        uid = os.environ.get("JUNIOR_UID", "")
        jid = os.environ.get("JUNIOR_ID", "")
        if token and uid and jid:
            if not any(a["token"] == token and a["uid"] == uid for a in self._accounts):
                self.add(token, uid, jid)

    def load_from_file(self):
        """Load accounts from persistent JSON file."""
        if ACCOUNTS_FILE.exists():
            try:
                data = json.loads(ACCOUNTS_FILE.read_text())
                for acct in data:
                    if not any(a["token"] == acct["token"] and a["uid"] == acct["uid"] for a in self._accounts):
                        self._accounts.append(acct)
            except (json.JSONDecodeError, KeyError):
                pass

    def _save(self):
        """Persist accounts to JSON file. Caller must hold _lock."""
        ACCOUNTS_FILE.write_text(json.dumps(self._accounts, indent=2))

    def add(self, token: str, uid: str, junior_id: str) -> int:
        """Add an account, returns its index."""
        with self._lock:
            self._accounts.append({"token": token, "uid": uid, "junior_id": junior_id})
            idx = len(self._accounts) - 1
            self._save()
        return idx

    def remove(self, index: int) -> bool:
        """Remove account by index."""
        with self._lock:
            if 0 <= index < len(self._accounts):
                self._accounts.pop(index)
                if self._index >= len(self._accounts):
                    self._index = 0
                self._save()
                return True
        return False

    def next(self) -> tuple[str, str, str] | None:
        """Get next account in round-robin order."""
        with self._lock:
            if not self._accounts:
                return None
            acct = self._accounts[self._index % len(self._accounts)]
            self._index = (self._index + 1) % len(self._accounts)
            return acct["token"], acct["uid"], acct["junior_id"]

    def list_all(self) -> list[dict]:
        """List all accounts (masked tokens for security)."""
        with self._lock:
            result = []
            for i, acct in enumerate(self._accounts):
                t = acct["token"]
                masked_token = t[:6] + "..." + t[-4:] if len(t) > 10 else "***"
                result.append({
                    "index": i,
                    "uid": acct["uid"],
                    "junior_id": acct["junior_id"],
                    "token": masked_token,
                })
            return result

    @property
    def count(self) -> int:
        with self._lock:
            return len(self._accounts)


pool = AccountPool()
pool.load_from_env()
pool.load_from_file()


def parse_auth(request: Request) -> tuple[str, str, str]:
    """Extract credentials: explicit Bearer token > round-robin pool."""
    auth = request.headers.get("authorization", "")
    if auth.startswith("Bearer "):
        parts = auth[7:].split("|")
        if len(parts) == 3:
            return parts[0], parts[1], parts[2]
    result = pool.next()
    if result:
        return result
    return "", "", ""


# ── Account Management Endpoints (admin-protected) ──────────
@app.get("/accounts")
async def list_accounts(request: Request):
    """List all accounts in the pool."""
    denied = await require_admin(request)
    if denied:
        return denied
    return {
        "total": pool.count,
        "accounts": pool.list_all(),
    }


@app.post("/accounts")
async def add_account(request: Request):
    """Add a new account. Body: {"token": ..., "uid": ..., "junior_id": ...}"""
    denied = await require_admin(request)
    if denied:
        return denied
    body = await request.json()
    token = body.get("token", "").strip()
    uid = body.get("uid", "").strip()
    junior_id = body.get("junior_id", "").strip()
    if not token or not uid or not junior_id:
        return JSONResponse(status_code=400, content={
            "error": "Missing required fields: token, uid, junior_id"
        })
    idx = pool.add(token, uid, junior_id)
    return {"message": "Account added", "index": idx, "total": pool.count}


@app.delete("/accounts/{index}")
async def remove_account(index: int, request: Request):
    """Remove account by index."""
    denied = await require_admin(request)
    if denied:
        return denied
    if pool.remove(index):
        return {"message": "Account removed", "total": pool.count}
    return JSONResponse(status_code=404, content={"error": "Account not found"})


def junior_headers(token: str, uid: str, junior_id: str) -> dict:
    return {
        "token": token,
        "uid": uid,
        "junior-id": junior_id,
        "user-agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/146.0.0.0 Safari/537.36"
        ),
    }


async def get_or_create_conversation(token: str, uid: str, junior_id: str) -> str:
    """Get existing conversation or let junior.so create one automatically."""
    headers = junior_headers(token, uid, junior_id)
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(f"{JUNIOR_BASE}/c/conversations", headers=headers)
        resp.raise_for_status()
        data = resp.json()
        convos = data.get("data", [])
        if convos:
            return convos[0]["id"]
    return "new"


async def send_message_to_junior(
    conv_id: str, text: str, token: str, uid: str, junior_id: str
) -> AsyncIterator[str]:
    """Send message to junior.so and yield SSE lines (async)."""
    headers = junior_headers(token, uid, junior_id)
    boundary = "JuniorProxy" + uuid.uuid4().hex[:8]
    body = (
        f"------{boundary}\r\n"
        f'Content-Disposition: form-data; name="text"\r\n\r\n'
        f"{text}\r\n"
        f"------{boundary}--\r\n"
    )
    headers["content-type"] = f"multipart/form-data; boundary=----{boundary}"

    async with httpx.AsyncClient(timeout=180) as client:
        async with client.stream(
            "POST",
            f"{JUNIOR_BASE}/c/conversations/{conv_id}/messages",
            headers=headers,
            content=body.encode(),
        ) as resp:
            resp.raise_for_status()
            async for line in resp.aiter_lines():
                if line:
                    yield line


# ── /v1/models ──────────────────────────────────────────────
@app.get("/v1/models")
async def list_models():
    return {
        "object": "list",
        "data": [
            {
                "id": "junior",
                "object": "model",
                "created": int(time.time()),
                "owned_by": "junior.so",
            }
        ],
    }


# ── /v1/chat/completions ───────────────────────────────────
@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    token, uid, junior_id = parse_auth(request)
    if not token:
        return JSONResponse(
            status_code=401,
            content={
                "error": {
                    "message": (
                        "Missing credentials. Pass Authorization: Bearer "
                        "<token>|<uid>|<junior_id> or set JUNIOR_TOKEN, "
                        "JUNIOR_UID, JUNIOR_ID env vars."
                    ),
                    "type": "auth_error",
                }
            },
        )

    body = await request.json()
    messages = body.get("messages", [])
    stream = body.get("stream", False)

    text_parts = []
    for msg in messages:
        role = msg.get("role", "user")
        content = msg.get("content", "")
        if role == "system":
            text_parts.append(f"[System]: {content}")
        elif role == "user":
            text_parts.append(content)
        elif role == "assistant":
            text_parts.append(f"[Previous response]: {content}")
    text = "\n".join(text_parts)

    conv_id = await get_or_create_conversation(token, uid, junior_id)

    completion_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
    created = int(time.time())

    if stream:
        return StreamingResponse(
            _stream_response(conv_id, text, token, uid, junior_id, completion_id, created),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )
    else:
        return await _non_stream_response(
            conv_id, text, token, uid, junior_id, completion_id, created
        )


async def _non_stream_response(
    conv_id: str,
    text: str,
    token: str,
    uid: str,
    junior_id: str,
    completion_id: str,
    created: int,
):
    """Collect full response and return as a single JSON."""
    full_text = ""
    input_tokens = 0
    output_tokens = 0

    async for line in send_message_to_junior(conv_id, text, token, uid, junior_id):
        if line.startswith("data: "):
            try:
                data = json.loads(line[6:])
                if "text" in data:
                    full_text += data["text"]
                elif "input_tokens" in data:
                    input_tokens = data["input_tokens"]
                    output_tokens = data["output_tokens"]
            except json.JSONDecodeError:
                pass

    return {
        "id": completion_id,
        "object": "chat.completion",
        "created": created,
        "model": "junior",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": full_text},
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": input_tokens,
            "completion_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens,
        },
    }


async def _stream_response(
    conv_id: str,
    text: str,
    token: str,
    uid: str,
    junior_id: str,
    completion_id: str,
    created: int,
):
    """Stream OpenAI-format SSE chunks."""
    async for line in send_message_to_junior(conv_id, text, token, uid, junior_id):
        if not line.startswith("data: "):
            continue
        try:
            data = json.loads(line[6:])
        except json.JSONDecodeError:
            continue

        if "text" in data:
            chunk = {
                "id": completion_id,
                "object": "chat.completion.chunk",
                "created": created,
                "model": "junior",
                "choices": [
                    {
                        "index": 0,
                        "delta": {"content": data["text"]},
                        "finish_reason": None,
                    }
                ],
            }
            yield f"data: {json.dumps(chunk)}\n\n"

        elif "input_tokens" in data:
            chunk = {
                "id": completion_id,
                "object": "chat.completion.chunk",
                "created": created,
                "model": "junior",
                "choices": [
                    {
                        "index": 0,
                        "delta": {},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": data.get("input_tokens", 0),
                    "completion_tokens": data.get("output_tokens", 0),
                    "total_tokens": data.get("input_tokens", 0)
                    + data.get("output_tokens", 0),
                },
            }
            yield f"data: {json.dumps(chunk)}\n\n"

    yield "data: [DONE]\n\n"


# ── Health check ────────────────────────────────────────────
@app.get("/health")
async def health():
    return {"status": "ok"}
