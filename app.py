import streamlit as st
from google import genai
from google.genai import types
from indian_skills import (
    scan_indian_stocks,
    analyze_entry_exit,
    calculate_risk_and_budget,
    get_indian_stock_news,
    get_stock_fundamentals,
    run_timemachine_backtest,
    create_paper_order_ticket,
)

st.set_page_config(page_title="Indian Day-Trader AI", page_icon="🇮🇳", layout="wide")
st.title("🇮🇳 Indian Market Day-Trader AI")
st.caption("NSE/BSE Intraday Scanner, Entry/Exit, Risk & Paper Broker — Powered by Gemini")

api_key = st.secrets.get("GEMINI_API_KEY", "")
if not api_key:
    st.error("Streamlit Secrets lo GEMINI_API_KEY add cheyandi.")
    st.stop()

client = genai.Client(api_key=api_key)

SYSTEM_INSTRUCTION = """
You are an expert Indian Stock Market (NSE/BSE) Algorithmic Day-Trading Assistant.
Rules:
1. Always respond using Indian Rupees (₹) and NSE symbols (e.g., RELIANCE, HDFCBANK, NIFTY, BANKNIFTY).
2. Market hours are 9:15 AM to 3:30 PM IST.
3. Automatically chain multiple tools when needed:
   - If user asks for a trade setup on a stock, call `analyze_entry_exit`, `get_indian_stock_news`, and `calculate_risk_and_budget` (assume ₹50,000 capital and 1% risk if not specified).
   - If user asks for best stocks today, call `scan_indian_stocks` first, then analyze the top mover.
4. Present results cleanly with Entry, Stop-Loss, Target (1:2 & 1:3 R:R), Qty, and Bias.
5. Remind user this is for educational/paper-trading analysis only.
"""

TOOLS = [
    scan_indian_stocks,
    analyze_entry_exit,
    calculate_risk_and_budget,
    get_indian_stock_news,
    get_stock_fundamentals,
    run_timemachine_backtest,
    create_paper_order_ticket,
]

with st.sidebar:
    st.header("⚡ Quick Commands")
    quick_prompt = None
    if st.button("🔍 Scan Top NSE Movers"):
        quick_prompt = "Scan Indian NSE stocks and tell me the top 3 intraday setups."
    if st.button("📊 Analyze RELIANCE"):
        quick_prompt = "RELIANCE entry, exit, news, and risk check for ₹50,000 capital."
    if st.button("📈 Analyze NIFTY"):
        quick_prompt = "NIFTY intraday bias, VWAP, EMA and RSI analysis cheyyi."
    if st.button("⏳ Backtest TATAMOTORS"):
        quick_prompt = "TATAMOTORS 30 days EMA backtest run chesi report ivvu."
    if st.button("🗑️ Clear Chat"):
        st.session_state.messages = []
        st.rerun()

if "messages" not in st.session_state:
    st.session_state.messages = []

for msg in st.session_state.messages:
    with st.chat_message(msg["role"]):
        st.markdown(msg["content"])

user_input = st.chat_input("Ask anything: e.g. 'SBIN entry & ₹1,00,000 capital risk check cheyyi'")
prompt = quick_prompt or user_input

if prompt:
    st.session_state.messages.append({"role": "user", "content": prompt})
    with st.chat_message("user"):
        st.markdown(prompt)

    with st.chat_message("assistant"):
        with st.spinner("Gemini Indian market tools run chesthundi..."):
            try:
                response = client.models.generate_content(
                    model="gemini-2.5-flash",
                    contents=prompt,
                    config=types.GenerateContentConfig(
                        system_instruction=SYSTEM_INSTRUCTION,
                        tools=TOOLS,
                        automatic_function_calling=types.AutomaticFunctionCallingConfig(
                            disable=False,
                            maximum_remote_calls=6,
                        ),
                    ),
                )
                reply = response.text or "No response generated."
            except Exception as e:
                reply = f"Error: {str(e)}"

            st.markdown(reply)
            st.session_state.messages.append({"role": "assistant", "content": reply})
