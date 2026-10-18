import asyncio
import hashlib
import json
import re
import time
import uuid
from contextlib import asynccontextmanager
from typing import AsyncGenerator, Dict, List, Tuple

import redis.asyncio as redis
import tiktoken
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

# =====================================================================
# HOUR 1: ATOMIC REDIS LUA SCRIPTS (Sliding-Window TPM)
# =====================================================================

LUA_RESERVE_TPM = """
local key = KEYS[1]
local now = tonumber(ARGV[1])
local window = tonumber(ARGV[2])
local limit = tonumber(ARGV[3])
local reserve_tokens = tonumber(ARGV[4])
local req_id = ARGV[5]

redis.call('ZREMRANGEBYSCORE', key, '-inf', now - window)

local members = redis.call('ZRANGE', key, 0, -1)
local current_usage = 0
for _, member in ipairs(members) do
    local sep = string.find(member, ":")
    if sep then
        local tokens = tonumber(string.sub(member, sep + 1))
        current_usage = current_usage + tokens
    end
end

if (current_usage + reserve_tokens) > limit then
    return {0, current_usage}
end

local member_val = req_id .. ":" .. reserve_tokens
redis.call('ZADD', key, now, member_val)
redis.call('PEXPIRE', key, window + 5000)

return {1, current_usage + reserve_tokens}
"""

LUA_RECONCILE_TPM = """
local key = KEYS[1]
local req_id = ARGV[1]
local reserved_tokens = tonumber(ARGV[2])
local actual_tokens = tonumber(ARGV[3])
local original_ts = tonumber(ARGV[4])

local old_member = req_id .. ":" .. reserved_tokens
local removed = redis.call('ZREM', key, old_member)

if removed == 1 and actual_tokens > 0 then
    local new_member = req_id .. ":" .. actual_tokens
    redis.call('ZADD', key, original_ts, new_member)
end

return actual_tokens
"""

redis_client: redis.Redis = None
reserve_sha: str = None
reconcile_sha: str = None
tokenizer = tiktoken.get_encoding("cl100k_base")

TENANT_QUOTAS = {
    "team-alpha": 1000,
    "team-payments": 50000,
}

@asynccontextmanager
async def lifespan(app: FastAPI):
    global redis_client, reserve_sha, reconcile_sha
    redis_client = redis.Redis(host="localhost", port=6379, decode_responses=True)
    reserve_sha = await redis_client.script_load(LUA_RESERVE_TPM)
    reconcile_sha = await redis_client.script_load(LUA_RECONCILE_TPM)
    yield
    await redis_client.aclose()

app = FastAPI(title="Enterprise AI Gateway (Hours 1-4 Complete)", lifespan=lifespan)

class Message(BaseModel):
    role: str
    content: str

class ChatCompletionRequest(BaseModel):
    model: str = "enterprise-reasoning-v1"
    messages: List[Message]
    max_tokens: int = Field(default=256, ge=1, le=4096)
    stream: bool = True

def count_chat_input_tokens(messages: List[Message]) -> int:
    num_tokens = 3
    for msg in messages:
        num_tokens += 4 + len(tokenizer.encode(msg.content))
    return num_tokens

# =====================================================================
# HOUR 3: SYNCHRONOUS PRE-FLIGHT GUARDRAILS (PII + Prompt Injection)
# =====================================================================

PII_PATTERNS = {
    "EMAIL": re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b", re.IGNORECASE),
    "PHONE": re.compile(r"(?:\b|\+)(?:91[-.\s]?)?[6-9]\d{2}[-.\s]?\d{3}[-.\s]?\d{4}\b|\b(?:\+?\d{1,3}[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}\b"),
    "PAN_CARD": re.compile(r"\b[A-Za-z]{5}[0-9]{4}[A-Za-z]{1}\b", re.IGNORECASE),
}

INJECTION_SIGNATURES = [
    re.compile(r"ignore (all )?(previous|prior) instructions", re.IGNORECASE),
    re.compile(r"reveal.*system prompt", re.IGNORECASE),
    re.compile(r"developer mode", re.IGNORECASE),
]

def run_preflight_guardrails(messages: List[Message]) -> Tuple[List[Message], Dict[str, str]]:
    pii_vault: Dict[str, str] = {}
    sanitized_messages: List[Message] = []
    counter = 1

    for msg in messages:
        content = msg.content
        for pattern in INJECTION_SIGNATURES:
            if pattern.search(content):
                raise HTTPException(
                    status_code=400,
                    detail={
                        "error": "GuardrailViolation",
                        "type": "PromptInjectionDetected",
                        "message": "Blocked by Layer-1 Pre-Flight Guardrail before token spend."
                    }
                )

        for pii_type, regex in PII_PATTERNS.items():
            def replace_match(match):
                nonlocal counter
                placeholder = f"<{pii_type}_{counter}>"
                pii_vault[placeholder] = match.group(0)
                counter += 1
                return placeholder
            content = regex.sub(replace_match, content)

        sanitized_messages.append(Message(role=msg.role, content=content))

    return sanitized_messages, pii_vault

# =====================================================================
# HOUR 2: MULTI-REGION CIRCUIT BREAKER & FAILOVER ROUTER
# =====================================================================

UPSTREAM_DEPLOYMENTS = [
    {"id": "bedrock-us-east-1", "simulate_fail": True},   # Simulates primary 503 outage
    {"id": "bedrock-us-west-2", "simulate_fail": False},  # Healthy secondary failover region
]

async def route_with_circuit_breaker() -> str:
    for dep in UPSTREAM_DEPLOYMENTS:
        dep_id = dep["id"]
        # Check if circuit breaker is already open in Redis
        if await redis_client.get(f"circuit_open:{dep_id}"):
            print(f"[ROUTER] Skipping {dep_id} (Circuit Open in Redis)")
            continue

        if dep["simulate_fail"]:
            # Trip circuit breaker in Redis for 45 seconds
            await redis_client.setex(f"circuit_open:{dep_id}", 45, "1")
            print(f"[CIRCUIT BREAKER TRIPPED] {dep_id} returned 503! Marked unhealthy for 45s.")
            continue

        return dep_id

    return UPSTREAM_DEPLOYMENTS[-1]["id"]

# =====================================================================
# HOUR 4: TENANT-ISOLATED CACHE & OPENTELEMETRY GENAI TRACING
# =====================================================================

def compute_tenant_cache_key(tenant_id: str, model: str, sanitized_messages: List[Message]) -> str:
    """
    Computes cache key based on the sanitized latest user query, 
    stripping all leading/trailing whitespace and internal newlines.
    """
    latest_query = ""
    for m in reversed(sanitized_messages):
        if m.role == "user":
            # Replace newlines with spaces and strip all whitespace
            latest_query = m.content.replace("\n", " ").strip().lower()
            break

    digest = hashlib.sha256(f"{model}:{latest_query}".encode()).hexdigest()[:16]
    cache_key = f"cache:tenant:{tenant_id}:{digest}"
    print(f"[CACHE DEBUG] Cleaned Query: '{latest_query}' | Generated Key: {cache_key}")
    return cache_key

def emit_otel_genai_span(
    req_id: str,
    tenant_id: str,
    region: str,
    cache_hit: bool,
    ttft_ms: float,
    total_ms: float,
    input_tokens: int,
    output_tokens: int,
    pii_masked: int
):
    """Emits a structured OpenTelemetry GenAI Semantic Convention span."""
    otel_span = {
        "trace_id": req_id,
        "span_name": "chat enterprise-reasoning-v1",
        "attributes": {
            "gen_ai.system": "aws.bedrock",
            "gen_ai.request.model": "enterprise-reasoning-v1",
            "gen_ai.response.region": region,
            "gen_ai.tenant.id": tenant_id,
            "gen_ai.cache.hit": cache_hit,
            "gen_ai.guardrail.pii_redacted_count": pii_masked,
            "gen_ai.usage.input_tokens": input_tokens,
            "gen_ai.usage.output_tokens": output_tokens,
            "gen_ai.server.time_to_first_token_ms": round(ttft_ms, 2),
            "gen_ai.client.operation.duration_ms": round(total_ms, 2),
        }
    }
    print(f"[OTEL GEN_AI SPAN] {json.dumps(otel_span)}")

# =====================================================================
# STREAMING GENERATORS (LIVE UPSTREAM + INSTANT CACHE REPLAY)
# =====================================================================

async def mock_upstream_llm_stream(prompt_tokens: int, max_tokens: int, user_content: str = "") -> AsyncGenerator[str, None]:
    """
    Simulates an upstream LLM, generating contextual responses based on the user's prompt.
    """
    content_lower = user_content.lower()
    
    if "vault" in content_lower or "err_vault_8200" in content_lower or "ping-ig-7f9b" in content_lower:
        response_text = "Per Runbook RB-402, verify Transit Gateway peering and rotate the AppRole secret ID before restarting the ping-ig-7f9b deployment."
    elif "contact" in content_lower or "email" in content_lower or "@" in content_lower:
        response_text = "Your contact information was successfully received and protected via Layer-1 PII redaction. How else can I assist with your infrastructure?"
    elif "diagnostic" in content_lower or "cluster" in content_lower or "health" in content_lower:
        response_text = "Cluster prod-ap-south-1 is currently DEGRADED. Pod ping-ig-7f9b is in CrashLoopBackOff due to a Vault timeout (ERR_VAULT_8200)."
    else:
        # FIXED: Streams back the actual user query instead of a cut-off slice
        response_text = f"Gateway processed your request successfully. Received query: '{user_content}'"

    words = response_text.split(" ")
    generated_tokens = 0

    for i, word in enumerate(words):
        if generated_tokens >= max_tokens:
            break
        await asyncio.sleep(0.03)
        generated_tokens += 1
        chunk_str = word + (" " if i < len(words) - 1 else "")
        chunk_payload = {
            "id": "chatcmpl-123",
            "object": "chat.completion.chunk",
            "choices": [{"index": 0, "delta": {"content": chunk_str}, "finish_reason": None}]
        }
        yield f"data: {json.dumps(chunk_payload)}\n\n"

    final_payload = {
        "id": "chatcmpl-123",
        "object": "chat.completion.chunk",
        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": generated_tokens,
            "total_tokens": prompt_tokens + generated_tokens
        }
    }
    yield f"data: {json.dumps(final_payload)}\n\n"
    yield "data: [DONE]\n\n"

async def cached_sse_replay(cached_text: str, req_id: str, tenant_id: str, start_time: float, pii_count: int):
    """Streams a cached response in <2ms without charging tenant TPM quota."""
    ttft_ms = (time.perf_counter() - start_time) * 1000
    chunk_payload = {
        "id": f"chatcmpl-cache-{req_id[:8]}",
        "object": "chat.completion.chunk",
        "choices": [{"index": 0, "delta": {"content": cached_text}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    }
    yield f"data: {json.dumps(chunk_payload)}\n\n"
    yield "data: [DONE]\n\n"
    total_ms = (time.perf_counter() - start_time) * 1000
    emit_otel_genai_span(req_id, tenant_id, "redis-edge-cache", True, ttft_ms, total_ms, 0, 0, pii_count)

async def _post_stream_tasks(
    redis_key: str,
    cache_key: str,
    req_id: str,
    tenant_id: str,
    routed_region: str,
    input_tokens: int,
    actual_output_tokens: int,
    reserved_tokens: int,
    reservation_ts: int,
    collected_text: List[str],
    ttft_ms: float,
    start_time: float,
    pii_count: int,
):
    """Detached background task: reconcile TPM, write cache, emit OTel span.
    Runs as asyncio.create_task() so it survives client disconnects."""
    # 1. Reconcile exact TPM in Redis
    try:
        actual_total = input_tokens + actual_output_tokens
        await redis_client.evalsha(
            reconcile_sha, 1, redis_key, req_id, reserved_tokens, actual_total, reservation_ts
        )
        print(f"[RECONCILED] Req: {req_id[:8]} | Reserved: {reserved_tokens} | Actual: {actual_total} | Refund: {reserved_tokens - actual_total}")
    except Exception as e:
        print(f"[TPM RECONCILE ERROR] {e}")

    # 2. Populate Tenant-Isolated Cache (TTL = 300s)
    try:
        if collected_text:
            cached_full_text = "".join(collected_text)
            await redis_client.setex(cache_key, 300, cached_full_text)
            print(f"[CACHE WRITE SUCCESS] Key: {cache_key} | {len(cached_full_text)} chars stored")
        else:
            print(f"[CACHE WRITE SKIPPED] No content collected for key: {cache_key}")
    except Exception as e:
        print(f"[CACHE WRITE ERROR] {e}")

    # 3. Emit OpenTelemetry Span
    try:
        total_ms = (time.perf_counter() - start_time) * 1000
        emit_otel_genai_span(
            req_id, tenant_id, routed_region, False,
            ttft_ms or total_ms, total_ms, input_tokens, actual_output_tokens, pii_count
        )
    except Exception as e:
        print(f"[OTEL ERROR] {e}")


async def rate_limited_sse_wrapper(
    upstream_gen: AsyncGenerator[str, None],
    redis_key: str,
    cache_key: str,
    req_id: str,
    tenant_id: str,
    routed_region: str,
    input_tokens: int,
    reserved_tokens: int,
    reservation_ts: int,
    start_time: float,
    pii_count: int
) -> AsyncGenerator[str, None]:
    actual_output_tokens = 0
    collected_text = []
    ttft_ms = None

    async for sse_line in upstream_gen:
        if ttft_ms is None:
            ttft_ms = (time.perf_counter() - start_time) * 1000

        if sse_line.startswith("data: ") and sse_line.strip() != "data: [DONE]":
            try:
                payload = json.loads(sse_line[6:].strip())
                if "usage" in payload and payload["usage"]:
                    # Final usage frame: capture definitive completion token count
                    actual_output_tokens = payload["usage"]["completion_tokens"]
                elif payload.get("choices") and payload["choices"][0].get("delta", {}).get("content"):
                    # Streaming chunk: accumulate text for cache write
                    token_str = payload["choices"][0]["delta"]["content"]
                    collected_text.append(token_str)
                    actual_output_tokens += 1
            except json.JSONDecodeError as e:
                print(f"[JSON Error] {e}")
        yield sse_line

    # Fire-and-forget post-stream work: safe against client disconnects
    asyncio.create_task(_post_stream_tasks(
        redis_key, cache_key, req_id, tenant_id, routed_region,
        input_tokens, actual_output_tokens, reserved_tokens, reservation_ts,
        collected_text, ttft_ms, start_time, pii_count
    ))

# =====================================================================
# UNIFIED GATEWAY INGRESS ENDPOINT
# =====================================================================
async def route_with_circuit_breaker() -> str:
    # Temporarily bypass random 503 chaos for testing cache
    return "bedrock-us-west-2"

@app.post("/v1/chat/completions")
async def chat_completions(
    body: ChatCompletionRequest,
    x_tenant_id: str = Header(..., description="Enterprise Tenant ID")
):
    start_time = time.perf_counter()
    if x_tenant_id not in TENANT_QUOTAS:
        raise HTTPException(status_code=403, detail=f"Unknown tenant: {x_tenant_id}")

    req_id = str(uuid.uuid4())

    # STEP 1: Pre-Flight Guardrails (PII Masking + Injection Defense)
    sanitized_messages, pii_vault = run_preflight_guardrails(body.messages)
    pii_count = len(pii_vault)
    if pii_vault:
        print(f"[PII GUARDRAIL] Redacted: {list(pii_vault.keys())}")

    # Extract latest user message for contextual mock LLM response
    latest_user_message = ""
    for msg in reversed(sanitized_messages):
        if msg.role == "user":
            latest_user_message = msg.content
            break

    # STEP 2: Check Tenant-Isolated Edge Cache (short-circuits before any TPM spend)
    cache_key = compute_tenant_cache_key(x_tenant_id, body.model, sanitized_messages)
    cached_response = await redis_client.get(cache_key)
    if cached_response:
        print(f"[CACHE HIT] Tenant: {x_tenant_id} | Key: {cache_key} | 0 TPM charged")
        return StreamingResponse(
            cached_sse_replay(cached_response, req_id, x_tenant_id, start_time, pii_count),
            media_type="text/event-stream",
            headers={"X-Request-ID": req_id, "X-Gateway-Cache": "HIT", "X-Routed-Region": "redis-edge-cache"}
        )
    print(f"[CACHE MISS] Key: {cache_key}")

    # STEP 3 (Hour 1): Atomic Redis Sliding-Window TPM Reservation
    tpm_limit = TENANT_QUOTAS[x_tenant_id]
    redis_key = f"tpm:tenant:{x_tenant_id}"
    now_ms = int(time.time() * 1000)
    input_tokens = count_chat_input_tokens(sanitized_messages)
    reserved_tokens = input_tokens + body.max_tokens

    allowed, current_window_tokens = await redis_client.evalsha(
        reserve_sha, 1, redis_key, now_ms, 60000, tpm_limit, reserved_tokens, req_id
    )
    if not allowed:
        raise HTTPException(status_code=429, detail="Sliding-window TPM quota exceeded.")

    # STEP 4 (Hour 2): Smart Multi-Region Router with Circuit Breaker
    routed_region = await route_with_circuit_breaker()

    upstream_stream = mock_upstream_llm_stream(input_tokens, body.max_tokens, latest_user_message)
    return StreamingResponse(
        rate_limited_sse_wrapper(
            upstream_stream, redis_key, cache_key, req_id, x_tenant_id,
            routed_region, input_tokens, reserved_tokens, now_ms, start_time, len(pii_vault)
        ),
        media_type="text/event-stream",
        headers={
            "X-Request-ID": req_id,
            "X-Gateway-Cache": "MISS",
            "X-Routed-Region": routed_region,
            "X-PII-Redacted-Count": str(len(pii_vault)),
        }
    )