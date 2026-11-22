import os
import shlex
import zipfile
import subprocess
import streamlit as st
from google import genai
from google.genai import types

st.set_page_config(page_title="Day-Trader Original Skills (Gemini)", page_icon="⚡", layout="wide")
st.title("⚡ Day-Trader Original Skills Engine")
st.caption("Runs your 100% original SKILL.md & Python scripts from zip — Powered by Gemini")

# 1. Auto-extract original zip on startup
ZIP_FILE = "day-trader-skills-install.zip"
EXTRACT_DIR = "extracted_skills"

if not os.path.exists(EXTRACT_DIR):
    if os.path.exists(ZIP_FILE):
        with zipfile.ZipFile(ZIP_FILE, "r") as z:
            z.extractall(EXTRACT_DIR)
    else:
        st.error(f"'{ZIP_FILE}' GitHub repo lo kanipinchaledu. Mundu aa zip file ni upload cheyandi.")
        st.stop()

# Map all 11 original skills from the zip
ORIGINAL_SKILLS = {
    "trader-scanner": "scan.py",
    "trader-entry": "entry.py",
    "trader-exit": "exit.py",
    "trader-risk": "check.py",
    "trader-budget": "size.py",
    "trader-news": "news.py",
    "trader-alerts": "alerts.py",
    "trader-analyst": "facts.py",
    "trader-broker": "ticket.py",
    "trader-pelosi": "ptr.py",
    "trader-timemachine": "timemachine.py",
}

api_key = st.secrets.get("GEMINI_API_KEY", "")
if not api_key:
    st.error("Streamlit Secrets lo GEMINI_API_KEY add cheyandi.")
    st.stop()

client = genai.Client(api_key=api_key)

# Tool 1: Read original SKILL.md (exact Claude Code behavior)
def read_skill_md(skill_name: str) -> str:
    """Reads the original SKILL.md file for a skill to learn its exact CLI commands, flags, and rules."""
    path = os.path.join(EXTRACT_DIR, skill_name, "SKILL.md")
    if not os.path.exists(path):
        return f"Error: {path} not found. Available skills: {list(ORIGINAL_SKILLS.keys())}"
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        return f.read()

# Tool 2: Execute original .py script with CLI arguments
def run_original_script(skill_name: str, cli_args: str = "--help") -> str:
    """Executes the original Python script from the zip with the given CLI arguments and returns stdout/stderr."""
    if skill_name not in ORIGINAL_SKILLS:
        return f"Error: Unknown skill '{skill_name}'. Available: {list(ORIGINAL_SKILLS.keys())}"

    script_file = ORIGINAL_SKILLS[skill_name]
    script_path = os.path.join(EXTRACT_DIR, skill_name, script_file)
    if not os.path.exists(script_path):
        return f"Error: Script not found at {script_path}"

    cmd = ["python", script_path] + (shlex.split(cli_args) if cli_args.strip() else [])
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        out = res.stdout.strip()
        err = res.stderr.strip()
        return f"COMMAND: {' '.join(cmd)}\nEXIT CODE: {res.returncode}\n\nSTDOUT:\n{out[:8000]}\n\nSTDERR:\n{err[:2000]}"
    except Exception as e:
        return f"Execution error: {str(e)}"

SYSTEM_INSTRUCTION = f"""
You are an autonomous trading agent (replacing Claude Code) that executes the user's ORIGINAL Python skills located in '{EXTRACT_DIR}'.
Available skills: {list(ORIGINAL_SKILLS.keys())}.

Strict Workflow (Exact Claude Code Process):
1. Whenever the user asks to run a task, FIRST call `read_skill_md(skill_name)` to read the exact instructions and CLI syntax of that original skill.
2. Then call `run_original_script(skill_name, cli_args)` using the exact CLI flags documented in SKILL.md (or `--help` if needed).
3. If the user asks about an Indian NSE stock (e.g., RELIANCE, SBIN, TCS), pass the NSE ticker format (e.g., RELIANCE.NS, SBIN.NS) to the original script.
4. Never make up numbers. Base your entire answer strictly on the actual script output.
"""

with st.sidebar:
    st.header("📂 Loaded Original Skills")
    for s in ORIGINAL_SKILLS:
        exists = os.path.exists(os.path.join(EXTRACT_DIR, s, ORIGINAL_SKILLS[s]))
        st.write(f"{'✅' if exists else '❌'} `{s}/{ORIGINAL_SKILLS[s]}`")
    if st.button("🗑️ Clear Chat"):
        st.session_state.messages = []
        st.rerun()

if "messages" not in st.session_state:
    st.session_state.messages = []

for msg in st.session_state.messages:
    with st.chat_message(msg["role"]):
        st.markdown(msg["content"])

user_prompt = st.chat_input("Example: 'trader-entry run chesi RELIANCE.NS check cheyyi' or 'trader-scanner run cheyyi'")

if user_prompt:
    st.session_state.messages.append({"role": "user", "content": user_prompt})
    with st.chat_message("user"):
        st.markdown(user_prompt)

    with st.chat_message("assistant"):
        with st.spinner("Original SKILL.md chadhivi script run chesthundi..."):
            try:
                response = client.models.generate_content(
                    model="gemini-3.8-flash",
                    contents=user_prompt,
                    config=types.GenerateContentConfig(
                        system_instruction=SYSTEM_INSTRUCTION,
                        tools=[read_skill_md, run_original_script],
                        automatic_function_calling=types.AutomaticFunctionCallingConfig(
                            disable=False,
                            maximum_remote_calls=8,
                        ),
                    ),
                )
                reply = response.text or "No response generated."
            except Exception as e:
                reply = f"Error: {str(e)}"

            st.markdown(reply)
            st.session_state.messages.append({"role": "assistant", "content": reply})
