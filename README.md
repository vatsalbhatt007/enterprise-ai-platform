# Enterprise AI Platform & Agentic Gateway Architecture

A production-grade, distributed AI platform architecture designed to transition large language models from unmanaged notebooks to secure, cost-optimized, and observable enterprise systems.

---

## Architecture Blueprint

[Client / Agent]
│ (OpenAI-Compatible REST / SSE Schema + Virtual Key JWT)
▼
┌─────────────────────────────────────────────────────────────┐
│ 1 & 2. AI GATEWAY (Ingress, TPM Rate Limit, PII & Circuit)  │
│  • Atomic Redis Lua Sliding-Window TPM & Reserve-Reconcile  │
│  • Pre-Flight PII Redaction & Prompt Injection Firewall     │
│  • Tenant-Isolated Semantic Edge Cache (<3ms Hit)           │
│  • Multi-Region Circuit Breaker & Automatic Failover        │
└──────┬──────────────────────────────────────────────┬───────┘
│                                              │
▼                                              ▼
┌──────────────────────────────┐    ┌─────────────────────────────────┐
│ 3. CONTEXT COMPACTOR (Box 3) │    │ 4. MULTI-STAGE RAG (Box 4)      │
│  • 100% Frozen Prefix Hash   │    │  • Pre-Retrieval RBAC ACLs      │
│  • Tool Output Pruning       │    │  • Hybrid Search (BM25 + Dense) │
│  • Sliding-Window Rollup     │    │  • Reciprocal Rank Fusion (RRF) │
│  • 80%+ Token Cost Reduction │    │  • Cross-Encoder Reranking      │
└──────────────────────────────┘    └─────────────────────────────────┘
│                                              │
└──────────────────────┬───────────────────────┘
▼
┌─────────────────────────────────────────────────────────────┐
│ 5 & 7. MCP RUNTIME & BLAST-RADIUS POLICY FIREWALL (Boxes 5/7) │
│  • JSON-RPC 2.0 Model Context Protocol Server               │
│  • Tier-1 Read-Only (Auto) vs. Tier-2 Mutating (HITL Pause) │
│  • Tier-3 Forbidden Destructive Action Hard Block           │
└─────────────────────────────┬───────────────────────────────┘
▼
┌─────────────────────────────────────────────────────────────┐
│ 8. EVALUATION & OBSERVABILITY BACKBONE (Box 8)              │
│  • OpenTelemetry GenAI Semantic Convention Spans            │
│  • Async RAG Triad LLM-as-a-Judge (Faithfulness & CoT)      │
│  • Human Calibration Queue & Cohen's Kappa (κ >= 0.80)      │
└─────────────────────────────────────────────────────────────┘

---

## Core Subsystems

### 1. Ingress & Gateway (`src/ai_gateway_tpm.py`)
* **Atomic Redis Lua Scripts:** Eliminates race conditions during high-concurrency agent tool calls by executing sliding-window Tokens-Per-Minute (TPM) checks atomically.
* **Reserve-Reconcile Pattern:** Over-reserves tokens on ingress based on `max_tokens`, then automatically refunds unused capacity the instant the Server-Sent Events (SSE) stream terminates.
* **Tenant-Isolated Edge Caching:** Partitions Redis semantic cache keys by `tenant_id` to prevent cross-tenant data leaks.

### 2. Context Engine (`src/context_engine.py`)
* **Prefix Cache Permanence:** Structures system instructions, sorted JSON tool schemas, and few-shot examples into an immutable prefix block (`fbeeca22ba4c`) to maximize upstream KV-cache hit rates.
* **Tool Output Pruning:** Collapses bulky historical JSON tool dumps into lightweight tombstones after turn execution.

### 3. Multi-Stage RAG Pipeline (`src/rag_engine.py`)
* **Zero-Trust RBAC:** Enforces document-level Access Control Lists (ACLs) *before* vector or sparse scoring occurs.
* **Hybrid Search + RRF:** Merges BM25 lexical precision with dense vector cosine similarity via Reciprocal Rank Fusion.
* **Indirect Prompt Injection Quarantine:** Detects adversarial instructions hidden inside retrieved documents and strips them before prompt assembly.

### 4. MCP Runtime & Policy Firewall (`src/mcp_runtime.py`)
* **JSON-RPC 2.0 Wire Protocol:** Standardizes tool discovery and execution via Model Context Protocol primitives (`tools/list`, `tools/call`, `resources/read`).
* **Deterministic Policy Engine:** OPA/Cedar-style Python firewall classifying actions into auto-approved reads, human-in-the-loop (HITL) escalations, and hard-blocked deletions[cite: 1, 2].

### 5. Async Evals & Calibration (`src/eval_judge_engine.py`)
* **Silent Drift Defense:** Runs asynchronous RAG Triad evaluations over production spans to catch LLM hallucinations that return polite `200 OK` statuses.
* **Cohen's Kappa Calibration:** Measures inter-rater reliability ($\kappa \ge 0.80$) between LLM judges and human SMEs[cite: 1, 2].

---

## Quickstart

### Prerequisites
```bash
pip install -r requirements.txt
```

### 1. Start Redis (Docker)
```bash
docker run -d --name local-redis -p 6379:6379 redis:alpine
# OR using docker compose (Redis + Gateway in containers):
docker compose up -d
```

### 2. Start the AI Gateway
```bash
cd src/
uvicorn ai_gateway_tpm:app --port 8000 --reload
# Swagger docs: http://localhost:8000/docs
```

### 3. Start the Streamlit Console UI (separate terminal)
```bash
cd src/
streamlit run app_ui.py --server.port 8501
# UI: http://localhost:8501
```

### 4. Run Standalone Analysis Modules
```bash
cd src/
python context_engine.py      # Prefix cache + sliding-window compaction demo
python rag_engine.py           # BM25 + dense vector + RRF + reranker demo
python mcp_runtime.py          # MCP blast-radius policy firewall demo
python eval_judge_engine.py    # LLM-as-a-Judge + Cohen's Kappa calibration demo
```

### 5. Test the Gateway (curl)
```bash
# First request → CACHE MISS, streams live response
curl -N -X POST http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" -H "X-Tenant-ID: team-payments" \
  -d '{"messages":[{"role":"user","content":"How do I fix ERR_VAULT_8200?"}],"max_tokens":128,"stream":true}'

# Second request → CACHE HIT, instant reply, 0 TPM charged
curl -si -X POST http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" -H "X-Tenant-ID: team-payments" \
  -d '{"messages":[{"role":"user","content":"How do I fix ERR_VAULT_8200?"}],"max_tokens":128,"stream":true}' \
  | grep "X-Gateway-Cache"
```
