import json
import requests
import streamlit as st

# 1. Page Configuration & Enterprise Styling
st.set_page_config(
    page_title="Enterprise AI Platform Console",
    page_icon="🛡️",
    layout="wide",
    initial_sidebar_state="expanded"
)

st.markdown("""
<style>
    /* Global Theme & Font Refinement */
    .stApp {
        background-color: #0e1117;
        color: #f0f2f6;
    }
    
    /* Top Header Styling */
    .enterprise-header {
        padding: 1.5rem 0 1rem 0;
        border-bottom: 1px solid #262730;
        margin-bottom: 2rem;
    }
    .enterprise-title {
        font-size: 1.8rem;
        font-weight: 700;
        letter-spacing: -0.02em;
        color: #ffffff;
        margin: 0;
    }
    .enterprise-subtitle {
        font-size: 0.95rem;
        color: #8a99ad;
        margin-top: 0.25rem;
    }

    /* Telemetry Metric Card Containers */
    div[data-testid="metric-container"] {
        background-color: #161b22;
        border: 1px solid #30363d;
        padding: 12px 16px;
        border-radius: 8px;
        box-shadow: 0 1px 3px rgba(0,0,0,0.12);
    }
    div[data-testid="metric-container"] label {
        color: #8a99ad !important;
        font-size: 0.8rem !important;
        text-transform: uppercase;
        letter-spacing: 0.05em;
    }
    div[data-testid="metric-container"] div[data-testid="stMetricValue"] {
        color: #58a6ff !important;
        font-size: 1.3rem !important;
        font-weight: 600;
    }

    /* Sidebar Refinement */
    section[data-testid="stSidebar"] {
        background-color: #111418;
        border-right: 1px solid #262730;
    }
    
    /* Chat Bubble Tweaks */
    .stChatMessage {
        padding: 1rem;
        border-radius: 8px;
        margin-bottom: 0.75rem;
        border: 1px solid transparent;
    }
    
    /* Input Box Polish */
    .stChatInputContainer {
        border-color: #30363d !important;
        border-radius: 10px !important;
    }
</style>
""", unsafe_allow_html=True)

# 2. Sidebar Layout & Configuration Controls
with st.sidebar:
    st.markdown("### 🎛️ Gateway Controls")
    gateway_url = st.text_input("API Gateway Endpoint", value="http://localhost:8000/v1/chat/completions")
    tenant_id = st.selectbox("Enterprise Tenant ID", ["team-alpha", "team-payments"])
    model_name = st.selectbox("Assigned LLM Model", ["enterprise-reasoning-v1", "claude-3-5-sonnet"])
    
    st.markdown("---")
    st.markdown("### 📊 Live Telemetry Stream")
    
    # Telemetry metrics slots
    col_s1, col_s2 = st.columns(2)
    with col_s1:
        metric_cache = st.metric("Cache", "---")
    with col_s2:
        metric_region = st.metric("Region", "---")
        
    metric_tokens = st.metric("Token Ledger", "In: 0 | Out: 0")
    metric_hash = st.metric("Prefix Fingerprint", "fbeeca22ba4c")
    
    st.markdown("---")
    if st.button("Clear Chat Session", use_container_width=True):
        st.session_state.messages = []
        st.rerun()

# 3. Main Header Area
st.markdown("""
<div class="enterprise-header">
    <div class="enterprise-title">🛡️ Enterprise Agentic Platform Console</div>
    <div class="enterprise-subtitle">Atomic Redis TPM Rate-Limiting &bull; Prefix-Cached KV Memory &bull; Deterministic MCP Firewall</div>
</div>
""", unsafe_allow_html=True)

# 4. Session State & Chat History Management
if "messages" not in st.session_state:
    st.session_state.messages = []

# Render conversation timeline
for message in st.session_state.messages:
    with st.chat_message(message["role"], avatar="🛡️" if message["role"] == "assistant" else "👤"):
        st.markdown(message["content"])

# 5. User Input Execution & SSE Stream Pipeline
if prompt := st.chat_input("Query platform runbooks, trigger EKS diagnostics, or test guardrails..."):
    st.session_state.messages.append({"role": "user", "content": prompt})
    with st.chat_message("user", avatar="👤"):
        st.markdown(prompt)

    payload = {
        "model": model_name,
        "messages": [{"role": m["role"], "content": m["content"]} for m in st.session_state.messages],
        "max_tokens": 512,
        "stream": True
    }
    headers = {
        "Content-Type": "application/json",
        "X-Tenant-ID": tenant_id
    }

    with st.chat_message("assistant", avatar="🛡️"):
        message_placeholder = st.empty()
        full_response = ""
        
        try:
            response = requests.post(gateway_url, json=payload, headers=headers, stream=True, timeout=30)
            
            if response.status_code != 200:
                try:
                    err_json = response.json()
                    err_msg = err_json.get("detail", response.text)
                except:
                    err_msg = response.text
                st.error(f"**Gateway Error ({response.status_code})**: `{err_msg}`")
            else:
                # Extract enterprise tracing headers
                cache_status = response.headers.get("X-Gateway-Cache", "MISS")
                routed_region = response.headers.get("X-Routed-Region", "unknown")
                pii_count = response.headers.get("X-PII-Redacted-Count", "0")
                
                # Update sidebar telemetry metrics dynamically
                metric_cache.metric("Cache", cache_status, delta="⚡ <3ms" if cache_status == "HIT" else None)
                metric_region.metric("Region", routed_region)
                if int(pii_count) > 0:
                    st.toast(f"🔒 Layer-1 Guardrail: Masked {pii_count} PII entities in flight.", icon="🔒")

                # Parse Server-Sent Events (SSE) stream
                for line in response.iter_lines():
                    if line:
                        decoded = line.decode("utf-8", errors="ignore")
                        if decoded.startswith("data: "):
                            raw_data = decoded[6:].strip()
                            if raw_data == "[DONE]":
                                break
                            try:
                                data_json = json.loads(raw_data)
                                choices = data_json.get("choices", [])
                                if choices:
                                    delta = choices[0].get("delta", {}).get("content", "")
                                    if delta:
                                        full_response += delta
                                        message_placeholder.markdown(full_response + "▌")
                                
                                # Capture final token usage frame
                                if "usage" in data_json and data_json["usage"]:
                                    usage = data_json["usage"]
                                    in_t = usage.get('prompt_tokens', 0)
                                    out_t = usage.get('completion_tokens', 0)
                                    metric_tokens.metric("Token Ledger", f"In: {in_t} | Out: {out_t}")
                            except json.JSONDecodeError:
                                pass

                message_placeholder.markdown(full_response)
                st.session_state.messages.append({"role": "assistant", "content": full_response})

        except requests.exceptions.ConnectionError:
            st.error("🚨 **Connection Refused**: Unable to reach FastAPI Gateway. Ensure `uvicorn` is active on port 8000.")