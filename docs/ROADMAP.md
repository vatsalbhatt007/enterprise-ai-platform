# Enterprise AI Platform — Next Implementation Roadmap

> **Current State (v1 — committed `bebdde9`):** Gateway + TPM + PII + Cache + RAG + MCP + Evals all working locally.
> **Goal:** Evolve from a hardcoded local demo into a production-grade, dynamically configured, cloud-deployable platform.

---

## Version Overview

```
v1 (done)  → Local demo, hardcoded config, mock LLM stream
v2         → Dynamic tenants via Postgres + Redis config sync
v3         → Real LLM proxy (OpenAI / Bedrock / Ollama via httpx)
v4         → OpenSearch RAG backend (replace in-memory KNOWLEDGE_BASE)
v5         → Cloud deployment (Railway / Render / AWS ECS)
```

---

## v2 — Dynamic Tenant & Config Management

### Problem
`TENANT_QUOTAS` in `ai_gateway_tpm.py` is hardcoded. Adding a new tenant requires a code change and redeployment.

### Solution Architecture
```
Admin API (FastAPI)           Postgres DB
  POST /tenants         →     tenants table
  PUT  /tenants/{id}    →       id, name, tpm_limit, rpm_limit, allowed_models, created_at
  DEL  /tenants/{id}    →

Postgres → (sync on write) → Redis Hash: HSET tenant:config:{id} tpm_limit 50000 ...
Gateway reads Redis (< 1ms), falls back to Postgres if cache miss
In-process TTL cache (30s) to avoid Redis round-trip on every request
```

### Files to Create / Modify
| File | Change |
|---|---|
| `src/config.py` | `pydantic-settings` model reading from `.env` — Redis URL, Postgres DSN, gateway port |
| `src/tenant_store.py` | `TenantStore` class: `get_tenant()`, `upsert_tenant()`, `delete_tenant()` backed by asyncpg + Redis |
| `src/ai_gateway_tpm.py` | Replace `TENANT_QUOTAS` dict with `await tenant_store.get_tenant(x_tenant_id)` call |
| `src/admin_api.py` | New FastAPI router: `POST /admin/tenants`, `GET /admin/tenants`, `PUT /admin/tenants/{id}` |
| `migrations/001_tenants.sql` | Postgres schema: `tenants` table |
| `docker-compose.yml` | Add `postgres` service |
| `requirements.txt` | Add `asyncpg`, `sqlalchemy[asyncio]` |

### Key Design Decisions
- **Redis as L1 cache, Postgres as source of truth** — same pattern LiteLLM uses for virtual keys
- **Write-through cache**: on `upsert_tenant()`, update Postgres first, then `HSET` Redis with 5-min TTL
- **Fallback**: if Redis key missing, gateway queries Postgres and re-populates cache
- **Admin API protected** by a static `ADMIN_API_KEY` env var (Bearer token header)

### Estimated effort: 1 session (~2–3 hours)

---

## v3 — Real LLM Proxy (Replace Mock Stream)

### Problem
`mock_upstream_llm_stream()` returns hardcoded strings. The platform cannot route to real AI providers.

### Solution Architecture
```
Gateway receives request
    │
    ├─ model = "gpt-4o"         → routes to OpenAI via httpx
    ├─ model = "claude-3-5"     → routes to Anthropic via httpx
    ├─ model = "llama3.2"       → routes to Ollama (local) via httpx
    └─ model = "bedrock/..."    → routes to AWS Bedrock via boto3
```

### Files to Create / Modify
| File | Change |
|---|---|
| `src/proxy.py` | `stream_from_upstream(model, messages, max_tokens, api_key)` — async httpx SSE proxy |
| `src/router.py` | `ModelRouter` — maps model name to provider + endpoint + API key from config |
| `src/ai_gateway_tpm.py` | Replace `mock_upstream_llm_stream()` call with `proxy.stream_from_upstream()` |
| `src/config.py` | Add `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `OLLAMA_BASE_URL`, `AWS_BEDROCK_REGION` |
| `requirements.txt` | Add `boto3`, `anthropic` (optional — or go httpx-only) |

### Key Design Decisions
- **Use `httpx.AsyncClient` with connection pooling** — reuse client across requests, set `timeout=60s`, `connect_timeout=5s`
- **Token refund on upstream 5xx**: if upstream returns non-200, call `LUA_RECONCILE_TPM` to return 100% reserved tokens
- **Upstream 5xx → circuit breaker**: trip Redis `circuit_open:{provider}` key to skip failed provider for 45s
- **Streaming pass-through**: `async for chunk in response.aiter_lines()` piped directly to client via `rate_limited_sse_wrapper`
- **LiteLLM compatibility**: accept `model` strings in LiteLLM format (`openai/gpt-4o`, `anthropic/claude-3-5-sonnet`)

### Estimated effort: 1 session (~2–3 hours)

---

## v4 — OpenSearch RAG Backend

### Problem
`KNOWLEDGE_BASE` in `rag_engine.py` is an in-memory Python list. Cannot scale past ~100 documents and requires redeployment to add/update runbooks.

### Solution Architecture
```
Document Ingestion Pipeline:
  PDF / Markdown / Confluence API
      → chunker (512 tokens, 64 overlap)
      → tiktoken token count
      → embedding model (text-embedding-3-small or local BGE)
      → OpenSearch index (BM25 + kNN vector field)

Query Pipeline (replaces current rag_engine.py):
  User query
      → ACL pre-filter (term query on allowed_roles field)
      → Hybrid search: BM25 (match) + kNN (dense_vector)
      → OpenSearch RRF (built-in from OS 2.11+)
      → Cross-encoder reranker (Cohere Rerank API or local BGE-reranker)
      → Context spotlighting + indirect injection scan
      → Return top-k chunks
```

### Files to Create / Modify
| File | Change |
|---|---|
| `src/rag_engine.py` | Replace in-memory `KNOWLEDGE_BASE` with `OpenSearchRAGEngine` class |
| `src/ingest.py` | New: document chunker + embedder + OpenSearch indexer CLI |
| `src/embedder.py` | New: `embed_text()` — calls OpenAI `text-embedding-3-small` or local `bge-m3` |
| `docker-compose.yml` | Add `opensearch` service (single-node dev mode) |
| `requirements.txt` | Add `opensearch-py`, `sentence-transformers` (optional for local embeddings) |

### Redis vs OpenSearch Decision
| Concern | Choice |
|---|---|
| < 500 documents | Stay with in-memory Python list (current v1) |
| 500–50K documents, keyword-heavy queries | OpenSearch BM25 only |
| 50K+ documents or semantic queries | OpenSearch BM25 + kNN hybrid |
| Ultra-low latency cache of top results | Redis + OpenSearch (cache hot queries) |

### Estimated effort: 1–2 sessions (~4–6 hours)

---

## v5 — Cloud Deployment

### Target: Railway (simplest) or AWS ECS (production)

#### Railway (Recommended for first deploy)
```
railway.app project
├── Gateway service    → Dockerfile → port 8000
├── Streamlit service  → streamlit run src/app_ui.py → port 8501
├── Redis plugin       → managed Redis, inject REDIS_URL env var
└── Postgres plugin    → managed Postgres, inject DATABASE_URL env var
```

**Steps:**
1. `railway login && railway init`
2. Add Redis + Postgres plugins from Railway dashboard
3. Set env vars: `REDIS_URL`, `DATABASE_URL`, `OPENAI_API_KEY`, `ADMIN_API_KEY`
4. `railway up` — auto-detects Dockerfile
5. Add `GATEWAY_URL=https://your-gateway.up.railway.app` to Streamlit service env

#### AWS ECS (Production)
```
ECR → ECS Fargate
├── gateway task    → 0.5 vCPU, 1GB RAM
├── streamlit task  → 0.25 vCPU, 512MB RAM
├── ElastiCache Redis (cluster mode DISABLED — required for Lua EVALSHA)
└── RDS Postgres (db.t4g.micro)
```

> **⚠️ Important:** ElastiCache must be `cluster mode disabled` — Redis Lua scripts (`EVALSHA`) are not supported in cluster mode.

#### Streamlit Production Hardening
- For internal teams: use **Streamlit Community Cloud** (free, GitHub-connected, secrets management built-in)
- For customer-facing: replace `app_ui.py` with a **Next.js** frontend consuming the FastAPI gateway directly

### Estimated effort: 0.5 session (~1–2 hours for Railway)

---

## Architecture Comparison: This Platform vs LiteLLM

| Capability | LiteLLM | This Platform |
|---|---|---|
| Multi-provider routing | ✅ | ✅ v3 |
| Virtual key / tenant management | ✅ Postgres | ✅ v2 Postgres |
| TPM rate limiting | Basic in-memory | ✅ Atomic Redis Lua (race-condition free) |
| Reserve-reconcile (streaming refund) | ❌ | ✅ v1 |
| PII redaction guardrail | ❌ | ✅ v1 |
| Prompt injection firewall | ❌ | ✅ v1 |
| Indirect RAG injection defense | ❌ | ✅ v1 |
| Tenant-isolated semantic cache | ❌ | ✅ v1 |
| MCP blast-radius firewall | ❌ | ✅ v1 |
| Human-in-the-loop (HITL) tool approval | ❌ | ✅ v1 |
| LLM-as-a-Judge evals | ❌ | ✅ v1 |
| Prefix-cache context compaction | ❌ | ✅ v1 |

---

## Suggested Git Branch & Commit Sequence

```bash
git checkout -b v2/dynamic-tenants
# ... implement tenant_store.py, admin_api.py, config.py, migrations/
git commit -m "feat(v2): dynamic tenant store via Postgres + Redis config sync"
git checkout main && git merge v2/dynamic-tenants

git checkout -b v3/real-llm-proxy
# ... implement proxy.py, router.py
git commit -m "feat(v3): httpx async LLM proxy — OpenAI, Anthropic, Ollama, Bedrock"
git checkout main && git merge v3/real-llm-proxy

git checkout -b v4/opensearch-rag
# ... implement ingest.py, embedder.py, update rag_engine.py
git commit -m "feat(v4): OpenSearch hybrid RAG backend with ingestion pipeline"
git checkout main && git merge v4/opensearch-rag

git checkout -b v5/cloud-deploy
# ... add Railway config, env hardening
git commit -m "feat(v5): Railway cloud deployment with managed Redis + Postgres"
git checkout main && git merge v5/cloud-deploy
```

---

*Last updated: 2026-10-18 | Platform v1 base commit: `bebdde9`*
