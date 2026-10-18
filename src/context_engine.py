import hashlib
import json
from typing import Dict, List, Tuple
import tiktoken
from pydantic import BaseModel

tokenizer = tiktoken.get_encoding("cl100k_base")

STATIC_SYSTEM_PROMPT = """You are an Enterprise Platform Engineering AI Agent.
Follow strict zero-trust execution policies:
1. Never execute destructive infrastructure changes without a verified change ticket.
2. Always cite internal runbook IDs when diagnosing cluster incidents.
3. Output structured diagnostic steps concisely."""

STATIC_MCP_TOOLS_SCHEMA = json.dumps([
    {
        "name": "query_eks_pod_logs",
        "description": "Fetches recent OpenTelemetry logs from an Amazon EKS pod.",
        "input_schema": {
            "type": "object",
            "properties": {
                "cluster": {"type": "string"},
                "namespace": {"type": "string"},
                "pod_name": {"type": "string"},
                "tail_lines": {"type": "integer", "default": 100}
            },
            "required": ["cluster", "namespace", "pod_name"]
        }
    },
    {
        "name": "check_route53_arc_routing",
        "description": "Checks active Route53 Application Recovery Controller (ARC) routing control states.",
        "input_schema": {
            "type": "object",
            "properties": {"control_panel_arn": {"type": "string"}},
            "required": ["control_panel_arn"]
        }
    }
], sort_keys=True)

STATIC_FEW_SHOT_EXAMPLES = """
Example 1:
User: Pod auth-ig-7f9b in prod-ap-south-1 is CrashLoopBackOff.
Assistant: [Calls query_eks_pod_logs(cluster="prod-ap-south-1", namespace="ciam", pod_name="auth-ig-7f9b")]
"""

class TurnMessage(BaseModel):
    role: str
    content: str
    is_raw_tool_dump: bool = False

class ConversationState(BaseModel):
    session_id: str
    rolling_summary: str = ""
    recent_turns: List[TurnMessage] = []
    total_raw_tokens_saved: int = 0

def count_tokens(text: str) -> int:
    return len(tokenizer.encode(text))

def prune_bulky_tool_output(raw_tool_json: str, max_chars: int = 180) -> Tuple[str, int]:
    orig_tokens = count_tokens(raw_tool_json)
    if len(raw_tool_json) <= max_chars:
        return raw_tool_json, 0
    compacted = f"[COMPACTED TOOL OUTPUT: {raw_tool_json[:max_chars]}... (Truncated {len(raw_tool_json) - max_chars} chars)]"
    saved_tokens = max(0, orig_tokens - count_tokens(compacted))
    return compacted, saved_tokens

def compact_sliding_window(state: ConversationState, max_recent_turns: int = 4) -> ConversationState:
    for i in range(len(state.recent_turns) - 1):
        msg = state.recent_turns[i]
        if msg.role == "tool" and msg.is_raw_tool_dump:
            pruned_text, saved = prune_bulky_tool_output(msg.content)
            msg.content = pruned_text
            msg.is_raw_tool_dump = False
            state.total_raw_tokens_saved += saved

    if len(state.recent_turns) > max_recent_turns:
        evicted_turns = state.recent_turns[:-max_recent_turns]
        state.recent_turns = state.recent_turns[-max_recent_turns:]
        evicted_tokens = sum(count_tokens(m.content) for m in evicted_turns)
        extracted_points = " | ".join(
            [f"{m.role.upper()}: {m.content[:80].strip()}..." for m in evicted_turns]
        )
        new_summary_block = f"Prior Context Checkpoint: [{extracted_points}]"
        if state.rolling_summary:
            state.rolling_summary = f"{state.rolling_summary}\n{new_summary_block}"
        else:
            state.rolling_summary = new_summary_block
        summary_tokens = count_tokens(new_summary_block)
        state.total_raw_tokens_saved += max(0, evicted_tokens - summary_tokens)

    return state

def assemble_prefix_cached_payload(
    state: ConversationState,
    dynamic_rag_context: str,
    current_user_query: str
) -> Dict:
    static_prefix_block = (
        f"{STATIC_SYSTEM_PROMPT}\n\n"
        f"## Available MCP Tools\n{STATIC_MCP_TOOLS_SCHEMA}\n\n"
        f"## Few-Shot Examples\n{STATIC_FEW_SHOT_EXAMPLES}"
    )
    prefix_hash = hashlib.sha256(static_prefix_block.encode()).hexdigest()[:12]
    cached_prefix_tokens = count_tokens(static_prefix_block)

    messages = [
        {
            "role": "system",
            "content": static_prefix_block,
            "cache_control": {"type": "ephemeral"}
        }
    ]

    if state.rolling_summary:
        messages.append({
            "role": "system",
            "content": f"## Compacted Session Memory\n{state.rolling_summary}"
        })

    for turn in state.recent_turns:
        messages.append({"role": turn.role, "content": turn.content})

    dynamic_tail = (
        f"<untrusted_rag_context>\n{dynamic_rag_context}\n</untrusted_rag_context>\n\n"
        f"User Query: {current_user_query}"
    )
    messages.append({"role": "user", "content": dynamic_tail})

    dynamic_tokens = sum(count_tokens(m["content"]) for m in messages[1:])
    uncached_cost_units = cached_prefix_tokens + dynamic_tokens + state.total_raw_tokens_saved
    optimized_cost_units = int(cached_prefix_tokens * 0.10) + dynamic_tokens
    cost_savings_pct = round((1 - (optimized_cost_units / max(1, uncached_cost_units))) * 100, 1)

    return {
        "prefix_cache_fingerprint": prefix_hash,
        "metrics": {
            "static_prefix_tokens_cached": cached_prefix_tokens,
            "dynamic_tail_tokens_billed": dynamic_tokens,
            "compaction_tokens_pruned": state.total_raw_tokens_saved,
            "estimated_cost_reduction_pct": f"{cost_savings_pct}%"
        },
        "messages": messages
    }

if __name__ == "__main__":
    state = ConversationState(session_id="inc-9941")
    bulky_eks_log = (
        '{"timestamp": "2026-10-18T14:20:00Z", "level": "ERROR", "pod": "ping-ig-7f9b", "stacktrace": "'
        + ("java.net.ConnectException: Vault connection timed out at com.enterprise.auth.VaultClient.fetchSecret(VaultClient.java:142) " * 25)
        + '"}'
    )

    simulated_turns = [
        TurnMessage(role="user", content="Check why ping-ig-7f9b is failing in ap-south-1."),
        TurnMessage(role="tool", content=bulky_eks_log, is_raw_tool_dump=True),
        TurnMessage(role="assistant", content="The pod is failing due to a HashiCorp Vault timeout on port 8200."),
        TurnMessage(role="user", content="Is Route53 ARC active in the secondary region?"),
        TurnMessage(role="assistant", content="Yes, ap-southeast-1 ARC routing control is healthy and ready."),
    ]

    for idx, turn in enumerate(simulated_turns, 1):
        state.recent_turns.append(turn)
        state = compact_sliding_window(state, max_recent_turns=3)
        payload = assemble_prefix_cached_payload(
            state,
            dynamic_rag_context="Runbook RB-402: If Vault times out in primary region, verify transit gateway peering before flipping ARC.",
            current_user_query=f"Turn {idx+1} follow-up question"
        )
        fp = payload["prefix_cache_fingerprint"]
        print(f"=== AFTER TURN {idx} ===")
        print(json.dumps(payload["metrics"], indent=2))
        print(f"Prefix Cache Hash (Must Never Change!): {fp}\n")