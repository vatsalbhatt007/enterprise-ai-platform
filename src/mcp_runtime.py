import json
import uuid
from typing import Any, Dict, List, Optional
from pydantic import BaseModel

# =====================================================================
# 1. DETERMINISTIC BACKEND MICROSERVICE STATE (FastAPI Layer Equivalent)
# =====================================================================

CLUSTER_STATE_DB = {
    "prod-ap-south-1": {
        "status": "DEGRADED",
        "active_pods": ["ping-ig-7f9b (CrashLoopBackOff)", "ping-am-4c2a (Running)"],
        "vault_peering": "TIMEOUT_ERR_8200",
        "arc_routing_state": "ACTIVE"
    },
    "prod-ap-southeast-1": {
        "status": "HEALTHY",
        "active_pods": ["ping-ig-9a1c (Running)", "ping-am-8b3d (Running)"],
        "vault_peering": "CONNECTED",
        "arc_routing_state": "STANDBY"
    }
}

# Simulated Redis / In-Memory HITL Pending Approval Queue
HITL_APPROVAL_QUEUE: Dict[str, Dict[str, Any]] = {}

# =====================================================================
# 2. MODEL CONTEXT PROTOCOL (MCP) SERVER REGISTRY (Resources & Tools)
# =====================================================================

MCP_RESOURCES = [
    {
        "uri": "infra://topology/multi-region-status",
        "name": "Multi-Region EKS & Route53 ARC Live Topology",
        "mimeType": "application/json",
        "description": "Read-only live state of ap-south-1 and ap-southeast-1 clusters."
    }
]

MCP_TOOLS = [
    {
        "name": "diagnose_cluster_health",
        "description": "SAFE READ-ONLY: Inspects pod statuses and Vault connectivity in a target EKS cluster.",
        "blast_radius_tier": "TIER_1_READ_ONLY",
        "inputSchema": {
            "type": "object",
            "properties": {"cluster_id": {"type": "string"}},
            "required": ["cluster_id"]
        }
    },
    {
        "name": "flip_route53_arc_failover",
        "description": "HIGH BLAST RADIUS: Shifts live production traffic between regions via Route53 ARC.",
        "blast_radius_tier": "TIER_2_HITL_REQUIRED",
        "inputSchema": {
            "type": "object",
            "properties": {
                "source_region": {"type": "string"},
                "target_region": {"type": "string"},
                "change_ticket_id": {"type": "string"}
            },
            "required": ["source_region", "target_region", "change_ticket_id"]
        }
    },
    {
        "name": "delete_production_cluster",
        "description": "CRITICAL DESTRUCTIVE: Permanently deletes an EKS cluster.",
        "blast_radius_tier": "TIER_3_FORBIDDEN_FOR_AGENTS",
        "inputSchema": {
            "type": "object",
            "properties": {"cluster_id": {"type": "string"}},
            "required": ["cluster_id"]
        }
    }
]

# =====================================================================
# 3. DETERMINISTIC BLAST-RADIUS POLICY ENGINE (Pre-Execution Firewall)
# =====================================================================

def evaluate_tool_policy(tool_name: str, arguments: Dict[str, Any], agent_role: str) -> Dict[str, Any]:
    """
    Deterministic policy-as-code check (OPA / Cedar style) BEFORE executing any MCP tool.
    Even if an LLM is jailbroken, it cannot bypass this deterministic Python gate.
    """
    tool_meta = next((t for t in MCP_TOOLS if t["name"] == tool_name), None)
    if not tool_meta:
        return {"decision": "DENY", "reason": f"Unknown tool '{tool_name}'"}

    tier = tool_meta["blast_radius_tier"]

    # Rule 1: Hard-block Tier 3 destructive tools from ever being called by an AI agent
    if tier == "TIER_3_FORBIDDEN_FOR_AGENTS":
        return {
            "decision": "DENY",
            "reason": f"Policy Violation: Tool '{tool_name}' is TIER_3_FORBIDDEN_FOR_AGENTS. Autonomous deletion is strictly prohibited."
        }

    # Rule 2: Tier 2 mutating tools require a valid CHG-* ticket format AND Human-in-the-Loop signoff
    if tier == "TIER_2_HITL_REQUIRED":
        ticket = arguments.get("change_ticket_id", "")
        if not ticket.startswith("CHG-"):
            return {
                "decision": "DENY",
                "reason": "Policy Violation: Mutating failover requires a valid 'CHG-XXXX' change_ticket_id."
            }
        return {
            "decision": "PAUSE_FOR_HITL",
            "reason": f"Tool '{tool_name}' modifies live regional routing. Escalating to SRE Human-in-the-Loop queue."
        }

    # Rule 3: Tier 1 Read-Only tools execute immediately
    return {"decision": "ALLOW", "reason": "Tier-1 Read-Only tool auto-approved."}

# =====================================================================
# 4. MCP JSON-RPC 2.0 WIRE PROTOCOL HANDLER
# =====================================================================

def handle_mcp_jsonrpc(request: Dict[str, Any], agent_role: str = "sre-agent") -> Dict[str, Any]:
    method = request.get("method")
    req_id = request.get("id", 1)
    params = request.get("params", {})

    # 1. MCP Discovery: List Tools
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": req_id, "result": {"tools": MCP_TOOLS}}

    # 2. MCP Discovery: Read Resource
    if method == "resources/read":
        uri = params.get("uri")
        if uri == "infra://topology/multi-region-status":
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "contents": [{"uri": uri, "mimeType": "application/json", "text": json.dumps(CLUSTER_STATE_DB)}]
                }
            }

    # 3. MCP Execution: Call Tool (Protected by Deterministic Policy Gate!)
    if method == "tools/call":
        tool_name = params.get("name")
        args = params.get("arguments", {})

        policy = evaluate_tool_policy(tool_name, args, agent_role)

        if policy["decision"] == "DENY":
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32001, "message": policy["reason"], "data": {"status": "BLOCKED_BY_FIREWALL"}}
            }

        if policy["decision"] == "PAUSE_FOR_HITL":
            approval_id = f"hitl-{str(uuid.uuid4())[:8]}"
            HITL_APPROVAL_QUEUE[approval_id] = {
                "tool_name": tool_name,
                "arguments": args,
                "status": "PENDING_HUMAN_REVIEW"
            }
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "status": "PAUSED_FOR_HITL_APPROVAL",
                    "approval_id": approval_id,
                    "message": policy["reason"],
                    "proposed_action": {"tool": tool_name, "args": args}
                }
            }

        # Execute Approved Tier-1 Tool
        if tool_name == "diagnose_cluster_health":
            cluster_data = CLUSTER_STATE_DB.get(args.get("cluster_id"), {"error": "Cluster not found"})
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {"status": "EXECUTED", "content": [{"type": "text", "text": json.dumps(cluster_data)}]}
            }

    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": -32601, "message": "Method not found"}}

def approve_hitl_action(approval_id: str, reviewer: str) -> Dict[str, Any]:
    """Human operator approves the paused MCP tool call to resume execution."""
    item = HITL_APPROVAL_QUEUE.get(approval_id)
    if not item:
        return {"error": "Approval ID not found"}

    args = item["arguments"]
    src, dst = args["source_region"], args["target_region"]
    CLUSTER_STATE_DB[src]["arc_routing_state"] = "STANDBY (DRAINED)"
    CLUSTER_STATE_DB[dst]["arc_routing_state"] = "ACTIVE (PRIMARY)"
    item["status"] = f"APPROVED_BY_{reviewer}"

    return {
        "status": "HITL_APPROVED_AND_EXECUTED",
        "approval_id": approval_id,
        "approved_by": reviewer,
        "updated_routing_state": {
            src: CLUSTER_STATE_DB[src]["arc_routing_state"],
            dst: CLUSTER_STATE_DB[dst]["arc_routing_state"]
        }
    }

if __name__ == "__main__":
    print("=== 1. AGENT CALLS TIER-1 READ-ONLY MCP TOOL (Auto-Approved) ===")
    res1 = handle_mcp_jsonrpc({
        "jsonrpc": "2.0", "id": 101, "method": "tools/call",
        "params": {"name": "diagnose_cluster_health", "arguments": {"cluster_id": "prod-ap-south-1"}}
    })
    print(json.dumps(res1, indent=2))

    print("\n=== 2. COMPROMISED AGENT ATTEMPTS TIER-3 DESTRUCTIVE TOOL (Hard-Blocked by Policy Firewall) ===")
    res2 = handle_mcp_jsonrpc({
        "jsonrpc": "2.0", "id": 102, "method": "tools/call",
        "params": {"name": "delete_production_cluster", "arguments": {"cluster_id": "prod-ap-south-1"}}
    })
    print(json.dumps(res2, indent=2))

    print("\n=== 3. AGENT CALLS TIER-2 FAILOVER TOOL (Paused for Human-in-the-Loop Signoff) ===")
    res3 = handle_mcp_jsonrpc({
        "jsonrpc": "2.0", "id": 103, "method": "tools/call",
        "params": {
            "name": "flip_route53_arc_failover",
            "arguments": {"source_region": "prod-ap-south-1", "target_region": "prod-ap-southeast-1", "change_ticket_id": "CHG-9042"}
        }
    })
    print(json.dumps(res3, indent=2))

    approval_id = res3["result"]["approval_id"]
    print(f"\n=== 4. HUMAN LEAD ARCHITECT APPROVES '{approval_id}' -> EXECUTES FAILOVER ===")
    res4 = approve_hitl_action(approval_id, reviewer="vp-platform-eng")
    print(json.dumps(res4, indent=2))