# JuniorGateway

OpenAI-compatible proxy server for [junior.so](https://junior.so) API.

Converts junior.so's proprietary chat API into standard `/v1/chat/completions` format, compatible with 9router and any OpenAI client.

## Features

- OpenAI-compatible `/v1/chat/completions` endpoint
- Streaming (SSE) and non-streaming responses
- **Multi-account support** with automatic round-robin rotation
- Runtime account management via REST API
- Persistent account storage (`accounts.json`)
- Token usage tracking

## Quick Start

```bash
# Install dependencies
pip install -e .

# Set credentials (single account)
export JUNIOR_TOKEN="your_token"
export JUNIOR_UID="your_uid"
export JUNIOR_ID="your_junior_id"

# Or multiple accounts
export JUNIOR_ACCOUNTS="token1|uid1|jid1,token2|uid2|jid2"

# Start server
uvicorn main:app --host 0.0.0.0 --port 8000
```

## Getting Credentials

1. Login to https://junior.so in your browser
2. Open DevTools Console (F12 → Console)
3. Paste this code:

```javascript
const session = JSON.parse(localStorage.getItem("junior-so-session"));
const juniorId = localStorage.getItem("junior-so-current-junior");
console.log("Token:", session.token);
console.log("UID:", session.uid);
console.log("Junior ID:", juniorId);
```

## API Endpoints

### Chat (OpenAI-compatible)

```bash
# Non-streaming
curl http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"junior","messages":[{"role":"user","content":"Hello!"}]}'

# Streaming
curl http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"junior","messages":[{"role":"user","content":"Hello!"}],"stream":true}'
```

### Models

```bash
curl http://localhost:8000/v1/models
```

### Account Management

```bash
# List accounts
curl http://localhost:8000/accounts

# Add account
curl -X POST http://localhost:8000/accounts \
  -H "Content-Type: application/json" \
  -d '{"token":"...","uid":"...","junior_id":"..."}'

# Remove account
curl -X DELETE http://localhost:8000/accounts/{index}
```

## 9router Integration

```
Base URL: http://localhost:8000/v1
API Key: any (uses round-robin from pool)
Model: junior
```

## Authentication

Two modes:
1. **Round-robin** (default): Requests automatically rotate through accounts in the pool
2. **Explicit**: Pass `Authorization: Bearer token|uid|junior_id` to use a specific account

## Test UI

Open http://localhost:8000/test for an interactive test page.
