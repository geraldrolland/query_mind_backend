# query_mind_backend

FastAPI backend for **QueryMind** — an AI-powered data analysis platform. It handles user authentication, CSV ingestion with automated cleaning, dataset storage on PostgreSQL, a custom JSON-B DSL query engine, and an LLM-powered chat assistant that answers questions about uploaded data with real numbers and charts.

## Features

- **Authentication** — email/password registration with email verification, login/logout with rotating access + refresh JWTs in signed cookies, CSRF protection, Google OAuth, and password reset via transactional email
- **Dataset pipeline** — CSV upload (up to 50 MB), automated cleaning (duplicate removal, per-column null counts and type inference), date enrichment (`<col>_day/_month/_year` derived columns), JSONB row storage
- **DSL query engine** — validate, compile, and execute structured query plans against JSONB rows directly in PostgreSQL (metrics, filters, sorts, grouping with date granularity day/month/quarter/year, limits)
- **AI chat assistant** — streaming NDJSON answers driven by an OpenAI-compatible LLM tool loop: `get_profile` → `validate_dsl_tool` → `generate_final_response`; records (charts) are deterministically enforced, answers are history-sanitized to force fresh queries, and all reported values come from executed queries
- **Middleware** — cookie-based authentication, per-user Redis response cache, CORS with credentials

## Tech Stack

| Layer | Technology |
| --- | --- |
| Framework | FastAPI 0.136 + Starlette 0.50 + Uvicorn |
| Language | Python 3.12 |
| ORM | SQLModel / SQLAlchemy 2.0 |
| Database | PostgreSQL (rows stored as JSONB) |
| Migrations | Alembic |
| Cache / tokens | Redis (verification tokens, OAuth state, response cache) |
| Auth | PyJWT + signed cookies, Argon2 password hashing, Google OAuth |
| LLM | OpenAI SDK (OpenAI-compatible endpoint, e.g. DeepSeek / NVIDIA NIM) |
| Data | pandas (cleaning), httpx |
| Tests | pytest + pytest-asyncio |

## Getting Started

### Prerequisites

- Python 3.12
- PostgreSQL (default port 5433, db `querymind`)
- Redis (default port 6380)

### 1. Configure environment

```bash
cp .env.example .env
```

Key variables (full list in `.env.example` and `app/core/settings.py`):

| Variable | Required | Description |
| --- | --- | --- |
| `SECRET_KEY` | Yes | Long random string used for JWT + cookie signing |
| `DATABASE_URL` | Yes | SQLAlchemy Postgres URL |
| `REDIS_URL` | Yes | Redis URL used for tokens and caching |
| `LLM_BASE_URL` | Yes | OpenAI-compatible API base (e.g. `https://api.deepseek.com`, NVIDIA NIM) |
| `LLM_API_KEY` | Yes | API key for the LLM provider |
| `LLM_MODEL` | Yes | Model id (e.g. `deepseek-v4-flash`) |
| `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` | Only for Google sign-in | OAuth web client credentials |
| `GOOGLE_REDIRECT_URI` | Only for Google sign-in | Must be registered exactly in the Google Cloud Console |
| `SMTP_*` | For email flows | SMTP server for verification/reset emails |
| `CORS_ORIGINS` | No | JSON list of allowed frontend origins |
| `MAX_UPLOAD_BYTES` | No | CSV upload limit (default 50 MB) |

### 2. Run

```bash
pip install -r requirements.txt
alembic upgrade head
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Interactive API docs: http://localhost:8000/docs

### 3. Test

```bash
pytest
```

## Project Structure

```
backend/
├── app/
│   ├── main.py                # FastAPI app, middleware, routers
│   ├── db.py                  # Engine + session factory + init_db
│   ├── redis_conf.py          # Redis client + ping
│   ├── auth/                  # JWT/cookie utils, Google OAuth code exchange
│   ├── cleaning/              # CSV cleaning pipeline + date enrichment
│   ├── core/                  # pydantic-settings configuration
│   ├── dsl/                   # DSL schema + postgresjsonBcompiler (compile & execute)
│   ├── email/                 # SMTP sender (verification, password reset)
│   ├── mcp/                   # LLM tool registry + executor (get_profile, validate_dsl_tool, ...)
│   ├── models/                # SQLModel models (user, dataset, datasetrow, message)
│   ├── profiler/              # Column statistics for get_profile
│   ├── routers/               # auth, datasets, chat
│   └── schemas/               # Pydantic request/response schemas + DSL validation
├── middleware/                # AuthenticationMiddleware, CacheMiddleware, CORS
├── alembic/                   # Migration scripts
├── tests/                     # pytest suite (smoke tests)
├── Dockerfile / entry_point.sh # alembic upgrade head + uvicorn on :8000
└── requirements.txt
```

## API Overview

| Endpoint | Description |
| --- | --- |
| `POST /api/v1/auth/register`, `verify-email`, `login`, `logout`, `refresh-token` | Full email/password auth flow with token rotation |
| `GET/POST /api/v1/auth/google/url`, `/google-auth` | Google OAuth flow (CSRF state token in Redis) |
| `POST /api/v1/auth/email`, `reset-password` | Password reset flow |
| `POST /api/v1/datasets/upload` | Upload CSV → cleaning report + enriched dataset |
| `GET /api/v1/datasets`, `/{id}`, `/{id}/records`, `/{id}/schema`, `/{id}/profile` | Dataset management + inspection |
| `DELETE /api/v1/datasets/{id}` | Delete dataset (cascades rows and chat history) |
| `POST /api/v1/datasets/{id}/query` | Validate, compile, execute a DSL plan (used by chat records) |
| `POST /api/v1/chat/{dataset_id}/query` | Stream an LLM answer as NDJSON events |
| `GET /health` | Liveness + Redis ping |

## Chat Assistant Protocol

1. The router builds a system prompt embedding the dataset schema and a strict ruleset, then runs an OpenAI-compatible tool loop (`MAX_TOOL_ITERATIONS`).
2. Tools: `get_profile` (statistics + sample rows), `validate_dsl_tool` (validates and executes one plan; returns real result rows), `generate_final_response` (final text + record blocks).
3. Deterministic safeguards:
   - Every successful `validate_dsl_tool` must be followed by a final response containing a record block with the exact validated DSL; text-only replies after tool use are bounced.
   - Record blocks without a this-turn validation are bounced.
   - Conversation history is sanitized (truncated text, placeholder records) so the model cannot replay stale numbers and must re-query.
4. Output is NDJSON: `{"type":"text"|"record", ...}` messages, `{"done":true}`, and error events; only final assistant messages are persisted.

## DSL Engine

Plans are JSON validated by Pydantic (`app/schemas/query.py`) then compiled to SQL by `postgresjsonBcompiler` against JSONB row data:

- `select` — plain field names (time bucketing only via `group_by`)
- `filters` — operators: `eq, neq, gt, gte, lt, lte, between, in, nin, contains, before, after`
- `metrics` — `sum, count, min, max, avg` with aliases
- `group_by` — field names or `{field, granularity: day|month|quarter|year}` date buckets
- `sorts`, `limit` — ordering and row cap
- Validation guarantees: `group_by` ⊆ `select` (plain names), metric fields exist, bucket expressions match the selected grouping mechanism

## Docker

```bash
docker build -t query-mind-backend .
docker run -p 8000:8000 \
  --env-file .env \
  -e DATABASE_URL=... -e REDIS_URL=... \
  query-mind-backend
```

The entrypoint runs `alembic upgrade head` before starting Uvicorn. In the full docker-compose stack the service connects to `postgres:5432` / `redis:6379` internally.

## Security

- Passwords hashed with Argon2; JWTs rotated on refresh; access tokens short-lived (default 15 min)
- Session stored in signed, HTTP-only cookies; CSRF tokens required on state-changing requests
- `.env` is git-ignored — secrets never committed
- CORS restricted to configured origins; user data queries are ownership-checked per request

## License

Private project.
