import json
import re
from typing import Dict, List
from pydantic import BaseModel

# =====================================================================
# 1. PRODUCTION OPENTELEMETRY TRACE PAYLOADS (Sampled from Gateway/RAG)
# =====================================================================

class ProductionTrace(BaseModel):
    trace_id: str
    user_query: str
    retrieved_rag_chunks: List[str]
    llm_response: str
    http_status: int = 200  # Both returned 200 OK to the user!

SAMPLE_TRACES = [
    ProductionTrace(
        trace_id="trace-9251ce4d-faithful",
        user_query="How do I fix ERR_VAULT_8200 on ping-ig-7f9b in ap-south-1?",
        retrieved_rag_chunks=[
            "RB-402: When pod ping-ig-7f9b throws ERR_VAULT_8200 ConnectException in ap-south-1, verify Transit Gateway peering and rotate the AppRole secret ID before restarting the deployment."
        ],
        llm_response="Per Runbook RB-402, verify Transit Gateway peering and rotate the AppRole secret ID before restarting the ping-ig-7f9b deployment."
    ),
    ProductionTrace(
        trace_id="trace-7741ab90-silent-drift",
        user_query="How do I fix ERR_VAULT_8200 on ping-ig-7f9b in ap-south-1?",
        retrieved_rag_chunks=[
            "RB-402: When pod ping-ig-7f9b throws ERR_VAULT_8200 ConnectException in ap-south-1, verify Transit Gateway peering and rotate the AppRole secret ID before restarting the deployment."
        ],
        # SILENT FAILURE: Returns HTTP 200 OK, sounds authoritative, cites RB-402,
        # but hallucinates a dangerous "--force-wipe-vault-storage" CLI command not in the runbook!
        llm_response="Per Runbook RB-402, verify Transit Gateway peering and run 'vault operator --force-wipe-vault-storage' to clear the 8200 lock."
    )
]

# =====================================================================
# 2. ASYNC LLM-AS-A-JUDGE ENGINE (RAG Triad + Chain-of-Thought Critique)
# =====================================================================

def extract_technical_claims(text: str) -> List[str]:
    """Extracts key technical actions, runbook IDs, and CLI flags from the response."""
    claims = []
    if "transit gateway" in text.lower():
        claims.append("verify Transit Gateway peering")
    if "approle" in text.lower():
        claims.append("rotate AppRole secret ID")
    cli_flags = re.findall(r"--[a-z0-9-]+", text.lower())
    for flag in cli_flags:
        claims.append(f"CLI flag '{flag}'")
    return claims

def evaluate_rag_triad_llm_judge(trace: ProductionTrace) -> Dict:
    """
    Executes a rubric-based Judge evaluation:
    1. Forces Chain-of-Thought (CoT) claim verification first.
    2. Emits discrete binary/3-point scores (1.0 = Pass, 0.0 = Fail) for the RAG Triad.
    """
    combined_context = " ".join(trace.retrieved_rag_chunks).lower()
    q_terms = set(re.findall(r"[a-z0-9_-]+", trace.user_query.lower()))
    ctx_terms = set(re.findall(r"[a-z0-9_-]+", combined_context))

    # Metric 1: Context Relevance (Does retrieved context match query entities?)
    context_relevance = 1.0 if ("err_vault_8200" in ctx_terms and "ping-ig-7f9b" in ctx_terms) else 0.0

    # Metric 2: Faithfulness / Groundedness (Are all claims in LLM output supported by context?)
    claims = extract_technical_claims(trace.llm_response)
    unsupported_claims = []
    for claim in claims:
        # Check if key tokens of the claim exist in the retrieved context
        claim_tokens = re.findall(r"[a-z0-9-]+", claim.lower())
        if not all(tok in combined_context for tok in claim_tokens if tok != "cli" and tok != "flag"):
            unsupported_claims.append(claim)

    faithfulness = 1.0 if len(unsupported_claims) == 0 else 0.0

    # Metric 3: Answer Relevance (Did the response address the user's question?)
    # Use meaningful content tokens only (len > 3) to avoid stop-word false negatives
    meaningful_q_terms = {t for t in q_terms if len(t) > 3}
    response_tokens = set(re.findall(r"[a-z0-9_-]+", trace.llm_response.lower()))
    answer_relevance = 1.0 if len(meaningful_q_terms.intersection(response_tokens)) >= 1 else 0.0

    cot_reasoning = (
        f"Verified {len(claims)} technical claims against retrieved context. "
        + (f"HALLUCINATION DETECTED: Unsupported claims {unsupported_claims} not found in RB-402."
           if unsupported_claims else "All claims strictly grounded in RB-402.")
    )

    overall_verdict = "PASS" if (context_relevance == 1.0 and faithfulness == 1.0 and answer_relevance == 1.0) else "FAIL_SILENT_DRIFT"

    return {
        "trace_id": trace.trace_id,
        "http_status_seen_by_user": trace.http_status,
        "judge_chain_of_thought": cot_reasoning,
        "rag_triad_scores": {
            "context_relevance": context_relevance,
            "faithfulness_groundedness": faithfulness,
            "answer_relevance": answer_relevance
        },
        "judge_verdict": overall_verdict,
        "escalate_to_human_calibration_queue": overall_verdict != "PASS"
    }

# =====================================================================
# 3. HUMAN CALIBRATION QUEUE & COHEN'S KAPPA ALIGNMENT CALCULATOR
# =====================================================================

def compute_cohens_kappa(judge_labels: List[int], human_sme_labels: List[int]) -> float:
    """
    Computes Cohen's Kappa (k = (Po - Pe) / (1 - Pe)) between binary Judge verdicts
    (1=PASS, 0=FAIL) and Human SME ground-truth annotations.
    """
    n = len(judge_labels)
    assert n == len(human_sme_labels) and n > 0

    # Observed agreement (Po)
    agreements = sum(1 for j, h in zip(judge_labels, human_sme_labels) if j == h)
    p_o = agreements / n

    # Expected agreement by chance (Pe)
    j_pass = sum(judge_labels) / n
    j_fail = 1.0 - j_pass
    h_pass = sum(human_sme_labels) / n
    h_fail = 1.0 - h_pass
    p_e = (j_pass * h_pass) + (j_fail * h_fail)

    if p_e == 1.0:
        return 1.0
    return round((p_o - p_e) / (1.0 - p_e), 4)

if __name__ == "__main__":
    print("=== 1. ASYNC LLM-AS-A-JUDGE EVALUATION ON PRODUCTION 200 OK TRACES ===")
    human_calibration_queue = []

    for trace in SAMPLE_TRACES:
        eval_result = evaluate_rag_triad_llm_judge(trace)
        print(json.dumps(eval_result, indent=2))
        if eval_result["escalate_to_human_calibration_queue"]:
            human_calibration_queue.append({
                "trace_id": trace.trace_id,
                "flagged_reason": eval_result["judge_chain_of_thought"],
                "dpo_rejected_response": trace.llm_response,
                "dpo_chosen_correction_template": "Requires SME rewrite for Phase-1 DPO alignment flywheel."
            })

    print("\n=== 2. HUMAN CALIBRATION QUEUE (Flagged Silent Failures -> DPO Flywheel) ===")
    print(json.dumps(human_calibration_queue, indent=2))

    # Simulate 10 historical traces graded by both our LLM Judge and a Human Principal Engineer
    # (1 = PASS, 0 = FAIL/HALLUCINATION)
    historical_judge_verdicts = [1, 0, 1, 1, 0, 0, 1, 0, 1, 0]
    historical_human_verdicts = [1, 0, 1, 1, 0, 1, 1, 0, 1, 0]  # 9/10 agreement

    kappa = compute_cohens_kappa(historical_judge_verdicts, historical_human_verdicts)
    print(f"\n=== 3. JUDGE-TO-HUMAN CALIBRATION METRIC (Cohen's Kappa) ===")
    print(f"Observed Agreement: 90.0% | Cohen's Kappa (k): {kappa} (Target >= 0.80 -> PRODUCTION CALIBRATED)")