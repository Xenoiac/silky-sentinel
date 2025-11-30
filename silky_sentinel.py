import os
import sys
import json
import time
import shutil
import subprocess
import requests
import re
from pathlib import Path

from dotenv import load_dotenv
from openai import OpenAI
from typing import TypedDict, List, Dict, Any, Optional
from types import SimpleNamespace

# --------------------------------------------------------------------
# Load .env from the same directory as this script
# --------------------------------------------------------------------
env_path = Path(__file__).resolve().parent / ".env"
if not env_path.exists():
    print(f"Warning: .env file not found at {env_path}")
load_dotenv(env_path)

# --------------------------------------------------------------------
# CONFIGURATION
# --------------------------------------------------------------------
OCI_REGION = os.getenv("OCI_REGION", "me-jeddah-1")
OCI_COMPARTMENT_OCID = os.getenv("OCI_COMPARTMENT_OCID")
SLACK_WEBHOOK_URL = os.getenv("SLACK_WEBHOOK_URL")
KUBECONFIG = os.getenv("KUBECONFIG")

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
LLM_MODEL = os.getenv("LLM_MODEL", "mistral-small3.2:latest")  # default model
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "openai").lower()
LLM_API_BASE = os.getenv("LLM_API_BASE")
LLM_TIMEOUT_SECONDS = int(os.getenv("LLM_TIMEOUT_SECONDS", "90"))


# OpenAI/ChatGPT uses strict JSON parsing, but some Ollama/Qwen models add prose around
# JSON payloads. This helper identifies when the backend is Ollama so downstream logic can
# relax parsing rules.
def is_ollama_backend() -> bool:
    provider = (LLM_PROVIDER or "").lower()
    if provider == "ollama":
        return True

    base_url = os.getenv("OPENAI_BASE_URL", OPENAI_BASE_URL) or ""
    if "ollama" in base_url.lower():
        return True

    api_base = os.getenv("LLM_API_BASE", LLM_API_BASE or "") or ""
    if "ollama" in api_base.lower():
        return True

    model_name = (LLM_MODEL or "").lower()
    if model_name.startswith("qwen"):
        return True

    return False

# Mode & night-mode config
SILKY_MODE = os.getenv("SILKY_MODE", "chat")
NIGHT_INTERVAL_SECONDS = int(os.getenv("NIGHT_INTERVAL_SECONDS", "300"))
NIGHT_LOG_PATH = Path(__file__).resolve().parent / "night_mode_events.log"
REPORTS_DIR = Path(__file__).resolve().parent / "reports"
AUDIT_LOG_PATH = Path(__file__).resolve().parent / "audit.log"

# --------------------------------------------------------------------
# LLM Clients
# --------------------------------------------------------------------


class OpenAIChatResponses:
    def __init__(self, client: OpenAI, default_model: str):
        self.client = client
        self.default_model = default_model

    def _normalize_messages(self, input: Any) -> List[Dict[str, str]]:
        if isinstance(input, list):
            return input
        if isinstance(input, dict):
            return [input]  # type: ignore[list-item]
        return [{"role": "user", "content": str(input)}]

    def create(self, model: str, input: Any, **kwargs: Any):
        messages = self._normalize_messages(input)
        completion = self.client.chat.completions.create(
            model=model or self.default_model,
            messages=messages,
            timeout=LLM_TIMEOUT_SECONDS,
            **kwargs,
        )
        try:
            content = completion.choices[0].message.content or ""
        except Exception:
            content = ""
        return SimpleNamespace(output_text=str(content), raw=completion)


class OpenAIChatClient:
    def __init__(self, api_key: str, base_url: str, default_model: str):
        self.client = OpenAI(api_key=api_key, base_url=base_url)
        self.responses = OpenAIChatResponses(self.client, default_model)


def _init_openai_client(api_key: Optional[str], base_url: str, allow_missing: bool = False):
    if api_key == "DUMMY_KEY_FOR_MOCK_DEMO":
        return None

    if not api_key:
        msg = "Error: OPENAI_API_KEY not found or invalid. Please update your .env file."
        if allow_missing:
            print(msg)
            return None
        print(msg)
        sys.exit(1)

    return OpenAIChatClient(api_key=api_key, base_url=base_url, default_model=LLM_MODEL)


def _init_llm_client():
    provider = (LLM_PROVIDER or "").lower()
    if provider not in {"openai", "ollama"}:
        print(
            f"Error: Unknown LLM_PROVIDER '{LLM_PROVIDER}'. Allowed providers are 'openai' and 'ollama'."
        )
        sys.exit(1)

    if provider == "openai":
        base_url = (os.getenv("OPENAI_BASE_URL", OPENAI_BASE_URL) or "https://api.openai.com/v1").rstrip("/")
        api_key = os.getenv("OPENAI_API_KEY", OPENAI_API_KEY)
        return _init_openai_client(api_key=api_key, base_url=base_url)

    api_base = (os.getenv("LLM_API_BASE") or LLM_API_BASE or "http://localhost:11434").rstrip("/")
    base_url = f"{api_base}/v1"
    api_key = os.getenv("OPENAI_API_KEY", "ollama") or "ollama"
    return _init_openai_client(api_key=api_key, base_url=base_url, allow_missing=True)


client = _init_llm_client()


# --------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------
def get_llm_client():
    """Return the configured LLM client, if available."""

    return client


def extract_text_from_responses(response: Any) -> str:
    """Safely extract text content from an LLM response object."""

    if response is None:
        return ""
    if hasattr(response, "output_text"):
        return getattr(response, "output_text") or ""
    try:
        return str(response)
    except Exception:
        return ""


def truncate_for_model(text: str, max_chars: int = 4000) -> str:
    """Trim long text so we don't blow the model context window."""
    if text is None:
        return ""
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + f"\n\n...[truncated, original length {len(text)} chars]..."


def _summarize_unified_context_for_prompt(context: Dict[str, Any]) -> str:
    """Generate a compact summary string for the system prompt."""

    if not context:
        return "No cluster context available yet."

    cluster = context.get("cluster_snapshot") or {}
    summary = cluster.get("summary") or {}
    namespaces = cluster.get("namespaces") or {}

    cpu = summary.get("cpu") or {}
    mem = summary.get("memory") or {}
    nodes = summary.get("nodes") or {}
    pods_summary = summary.get("pods") or {}

    cpu_line = (
        f"CPU {cpu.get('used_cores', 0)}/{cpu.get('total_cores', 0)} cores"
        f" ({cpu.get('utilization_percent', 0)}%)."
    )
    mem_line = (
        f"Memory {mem.get('used_gib', 0)}/{mem.get('total_gib', 0)} GiB"
        f" ({mem.get('utilization_percent', 0)}%)."
    )
    node_line = f"Nodes ready {nodes.get('ready', 0)}/{nodes.get('count', 0)}."
    pods_line = (
        f"Pods unhealthy {pods_summary.get('unhealthy', 0)}/"
        f"{pods_summary.get('total', 0)}."
    )

    def _format_namespace_list(items: List[Dict[str, Any]], label: str) -> Optional[str]:
        if not items:
            return None
        formatted = ", ".join(
            [
                f"{i.get('namespace', i.get('name', '?'))}"
                f"({i.get('value', i.get('cpu', i.get('memory', '?')))})"
                for i in items
            ]
        )
        return f"Top namespaces by {label}: {formatted}."

    ns_cpu = _format_namespace_list(namespaces.get("top_by_cpu") or [], "CPU")
    ns_mem = _format_namespace_list(namespaces.get("top_by_memory") or [], "memory")

    pod_inventory = cluster.get("pods") or []
    pod_line = f"Pod inventory tracked: {len(pod_inventory)} pods." if pod_inventory else None

    events = context.get("recent_events") or []
    events_line = f"Recent Night Mode events: {len(events)} entries." if events else "No recent Night Mode events."

    latest_report = truncate_for_model(context.get("latest_report", ""), max_chars=240)
    report_line = (
        f"Latest Night Mode report snippet: {latest_report}" if latest_report else "No Night Mode report available."
    )

    parts = [
        cpu_line,
        mem_line,
        node_line,
        pods_line,
        ns_cpu,
        ns_mem,
        pod_line,
        events_line,
        report_line,
    ]

    return " ".join([p for p in parts if p])


def build_sre_system_prompt(context: Optional[Dict[str, Any]] = None) -> str:
    """Build the Silky Sentinel persona prompt, with compact context."""

    unified_context = context.get("unified_context") if isinstance(context, dict) else None
    context_for_summary = unified_context if unified_context is not None else context

    base = (
        "You are Silky Sentinel, senior SRE/DevOps for a Kubernetes platform, cluster-aware and cost-aware. "
        "Keep answers short and clear (1-4 sentences) for SREs, developers, and management. "
        "Propose kubectl/shell commands when helpful. "
        "When summarizing, always state what was checked, what it means, and the single most important next step. "
    )

    if context_for_summary:
        base += f"Current context: {_summarize_unified_context_for_prompt(context_for_summary)}"

    base += (
        "You already have Silky Sentinel's cluster snapshots and Night Mode data; do not claim you lack access. "
        "Final answers must be short paragraphs that recap actions taken, findings, and exactly one next check/run step."
    )

    return base


def _latest_report_text_or_empty() -> str:
    if not REPORTS_DIR.exists():
        return ""

    candidates = [p for p in REPORTS_DIR.glob("**/*") if p.is_file()]
    if not candidates:
        return ""

    latest_file = max(candidates, key=lambda p: p.stat().st_mtime)
    try:
        return latest_file.read_text(errors="replace")
    except Exception:
        return ""


def _read_recent_night_events(limit: int = 15) -> List[Dict[str, Any]]:
    if not NIGHT_LOG_PATH.exists():
        return []

    entries: List[Dict[str, Any]] = []
    try:
        with open(NIGHT_LOG_PATH, "r") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except Exception:
        return []

    return list(reversed(entries[-limit:]))


def build_unified_sre_context() -> Dict[str, Any]:
    """
    Collect a compact context bundle for the SRE brain (cluster + night mode).

    Each piece is gathered defensively; failures simply yield empty values.
    """

    context: Dict[str, Any] = {
        "mode": SILKY_MODE,
        "cluster_snapshot": {},
        "recent_events": [],
        "latest_report": "",
    }

    if OPENAI_API_KEY == "TEST" or os.getenv("OPENAI_API_KEY") == "TEST" or os.getenv(
        "SILKY_SKIP_CONTEXT_COLLECTION", ""
    ).lower() == "true":
        return context

    try:
        ensure_kubeconfig()
    except Exception:
        pass

    try:
        snapshot = night_collect_cluster_health()
        context["cluster_snapshot"] = snapshot or {}
    except Exception:
        context["cluster_snapshot"] = {}

    try:
        events = _read_recent_night_events(limit=15)
        context["recent_events"] = events or []
    except Exception:
        context["recent_events"] = []

    try:
        latest_report = _latest_report_text_or_empty()
        context["latest_report"] = truncate_for_model(latest_report, max_chars=3000)
    except Exception:
        context["latest_report"] = ""

    return context


def ensure_kubeconfig() -> str:
    """Validate kubeconfig path and export it so kubectl always uses it."""
    kubeconfig = os.getenv("KUBECONFIG")

    if not kubeconfig:
        raise RuntimeError(
            "KUBECONFIG is not set in your .env file. Please add it there."
        )

    kube_path = Path(kubeconfig).expanduser().resolve()

    if not kube_path.exists():
        raise RuntimeError(
            f"KUBECONFIG path does not exist: {kube_path}. "
            "Fix your .env or ensure the file is present."
        )

    # Export it so subprocess commands (kubectl) inherit it
    os.environ["KUBECONFIG"] = str(kube_path)
    return str(kube_path)


# --------------------------------------------------------------------
# NOTIFICATION / LOGGING LAYER
# --------------------------------------------------------------------
def notify_admin(message, level="INFO"):
    """
    Sends a notification/log message.
    For now it's mostly logging to stdout; Slack is optional.
    """
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    formatted_msg = f"\n[{timestamp}] [{level}] 🤖 SILKY SENTINEL: {message}\n"
    print(formatted_msg)

    if SLACK_WEBHOOK_URL:
        try:
            payload = {"text": formatted_msg}
            requests.post(SLACK_WEBHOOK_URL, json=payload, timeout=5)
        except Exception as e:
            print(f"Failed to send webhook notification: {e}")


# --------------------------------------------------------------------
# AUDIT & SHELL COMMAND EXECUTION
# --------------------------------------------------------------------
def log_audit_entry(command: str, result: dict):
    """
    Append a JSON line to audit.log with what was run and when.
    This is global: Night Mode and chat-mode commands both get audited.
    """
    entry = {
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "command": command,
        "exit_code": result.get("exit_code"),
        "stdout_snippet": (result.get("stdout") or "")[:500],
        "stderr_snippet": (result.get("stderr") or "")[:500],
    }
    try:
        with open(AUDIT_LOG_PATH, "a") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception as e:
        print(f"Failed to write audit log: {e}")


def run_shell_command(command: str, timeout: Optional[int] = None, cwd: Optional[str] = None) -> dict:
    """
    Actually execute a shell command and capture stdout/stderr/exit code.
    This is only called AFTER user approval (in chat mode) or by Night Mode.
    """
    try:
        env = os.environ.copy()  # make sure KUBECONFIG is included

        result = subprocess.run(
            command,
            shell=True,
            text=True,
            capture_output=True,
            env=env,
            timeout=timeout,
            cwd=cwd,
        )
        data = {
            "exit_code": result.returncode,
            "stdout": result.stdout.strip(),
            "stderr": result.stderr.strip(),
        }

        # 🔐 Audit everything
        try:
            log_audit_entry(command, data)
        except Exception:
            pass
        return data

    except Exception as e:
        data = {
            "exit_code": -1,
            "stdout": "",
            "stderr": f"Exception while running command: {e}",
        }
        try:
            log_audit_entry(command, data)
        except Exception:
            pass
        return data


def apply_sre_suggestion(suggestion: Dict[str, Any]) -> Dict[str, Any]:
    """
    Execute a suggested command and ask the SRE brain to summarize the outcome.

    suggestion keys:
      - title: str
      - reason: str
      - action: str
      - command: str

    Returns:
      {
        "status": "ok" | "error",
        "summary": str,    # very short outcome summary
        "next_step": str,  # optional follow-up step (may be empty)
        "exit_code": int,
      }
    """
    cmd = (suggestion.get("command") or "").strip()
    context = {
        "suggestion_title": suggestion.get("title", ""),
        "suggestion_reason": suggestion.get("reason", ""),
        "suggestion_action": suggestion.get("action", ""),
        "command": cmd,
    }
    if not cmd:
        return {
            "status": "error",
            "summary": "No command is defined for this suggestion.",
            "next_step": "",
            "exit_code": -1,
        }

    # 1) Run the command
    result = run_shell_command(cmd)  # reuse your existing helper
    stdout_val = getattr(result, "stdout", None)
    stderr_val = getattr(result, "stderr", None)
    exit_code_val = getattr(result, "exit_code", None)

    if isinstance(result, dict):
        stdout_val = stdout_val if stdout_val is not None else result.get("stdout")
        stderr_val = stderr_val if stderr_val is not None else result.get("stderr")
        exit_code_val = exit_code_val if exit_code_val is not None else result.get("exit_code")

    stdout = (stdout_val or "")[:4000]
    stderr = (stderr_val or "")[:4000]
    exit_code = int(exit_code_val) if exit_code_val is not None else 0

    # 2) Ask the SRE brain to summarize
    system_prompt = build_sre_system_prompt(context)
    user_prompt = (
        "You recommended this action for a Kubernetes cluster, and we just ran the command.\n\n"
        f"Command: {cmd}\n"
        f"Exit code: {exit_code}\n\n"
        f"STDOUT (truncated):\n{stdout}\n\n"
        f"STDERR (truncated):\n{stderr}\n\n"
        "In 1-3 short sentences, describe:\n"
        "- what was checked or changed,\n"
        "- what the results show,\n"
        "- and whether any further action is needed (mention only one most important next step).\n"
    )

    llm_client = get_llm_client()
    summary = ""

    if llm_client is None:
        summary = "Command executed; LLM summarization unavailable."
    else:
        completion = llm_client.responses.create(
            model=LLM_MODEL,
            input=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        )
        summary = extract_text_from_responses(completion).strip()

    return {
        "status": "ok" if exit_code == 0 else "error",
        "summary": summary,
        "next_step": "",
        "exit_code": exit_code,
    }


# --------------------------------------------------------------------
# LOCAL LOG ANALYZER (Smart Log Refining)
# --------------------------------------------------------------------
def analyze_logs_locally(
    log_path: str,
    keywords=None,
    context_lines: int = 5,
    max_snippets: int = 50,
) -> str:
    """
    Scan a large log file locally, extract 'interesting' snippets, and
    return a human-readable digest (ready to show user + send to AI).

    - keywords: list of strings / regex fragments to detect (e.g. ["ERROR", "Exception"])
    - context_lines: how many lines before/after the hit to include
    - max_snippets: cap how many snippets we collect to avoid huge outputs
    """
    if keywords is None:
        keywords = ["ERROR", "Error", "Exception", "FATAL", "Fatal", "Panic", "Traceback", "503", "500"]

    if not os.path.exists(log_path):
        return f"[LOG ANALYZER] Log file not found: {log_path}"

    # Compile a single regex like: (ERROR|Exception|FATAL|...)
    pattern = re.compile("|".join(re.escape(k) for k in keywords))

    snippets = []
    total_lines = 0
    matched_lines = 0

    with open(log_path, "r", errors="replace") as f:
        lines = f.readlines()

    total_lines = len(lines)

    for i, line in enumerate(lines):
        if pattern.search(line):
            matched_lines += 1

            if len(snippets) >= max_snippets:
                continue

            start_idx = max(0, i - context_lines)
            end_idx = min(len(lines), i + context_lines + 1)
            snippet_lines = lines[start_idx:end_idx]

            snippet_text = "".join(snippet_lines).rstrip("\n")
            snippets.append(
                f"--- SNIPPET #{len(snippets) + 1} (line {i+1}) ---\n{snippet_text}\n"
            )

    header = [
        f"[LOG ANALYZER] File: {log_path}",
        f"Total lines scanned: {total_lines}",
        f"Lines matching patterns ({', '.join(keywords)}): {matched_lines}",
        f"Snippets returned (max {max_snippets}): {len(snippets)}",
        "",
    ]
    body = "\n".join(snippets) if snippets else "[LOG ANALYZER] No matching errors found."
    digest = "\n".join(header) + "\n" + body

    # Final safety valve: truncate before returning
    digest = truncate_for_model(digest, max_chars=8000)
    return digest


# --------------------------------------------------------------------
# AI REASONING CORE WITH COMMAND-EXECUTION & LOG-ANALYSIS LOOP
# --------------------------------------------------------------------
class AgentState(TypedDict):
    messages: List[Dict[str, Any]]
    max_steps: int
    steps_done: int


def init_agent_state(system_prompt: str, first_user_message: str, max_steps: int = 8) -> AgentState:
    return {
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": first_user_message},
        ],
        "max_steps": max_steps,
        "steps_done": 0,
    }


def build_system_prompt_for_agent(context: Optional[dict] = None) -> str:
    context_section = ""
    if context:
        context_section = (
            "Suggestion context:" "\n"
            f"- Title: {context.get('title','')}\n"
            f"- Reason: {context.get('reason','')}\n"
            f"- Action: {context.get('action','')}\n"
            f"- Command: {context.get('command','')}\n"
        )

    base_prompt = f"""
You are 'Silky Sentinel', a senior DevOps/SRE assistant for Silky Systems.

You are running in a special mode where you can:
- Propose concrete shell commands (kubectl, oci, bash, etc.).
- Request local log analysis on large files.
- Receive the actual outputs/digests back.
- Use that data to decide next steps.
- Eventually provide a final human-readable answer.

Cluster defaults:
- Default region: {OCI_REGION}
- Default compartment: {OCI_COMPARTMENT_OCID}

{context_section}

CRITICAL RULES:

1. YOU NEVER CLAIM YOU EXECUTED ANYTHING.
   The Python layer will optionally execute the commands **only if the user approves**.

2. YOU MUST ALWAYS RESPOND IN PURE JSON, WITH NO MARKDOWN OR EXTRA TEXT.
   There are only THREE allowed shapes:

   a) To request a command to be run:
      {{
        "action": "run_command",
        "command": "<the exact shell command>",
        "reason": "<short explanation why this command is needed>"
      }}

   b) To request local log analysis (NO shell command, just local file scan):
      {{
        "action": "analyze_log",
        "log_path": "<absolute path to the log file>",
        "keywords": ["ERROR", "Exception", "FATAL"],
        "context_lines": 5,
        "max_snippets": 50,
        "reason": "<short explanation why this analysis is needed>"
      }}

   - The keywords/context_lines/max_snippets fields are optional;
     if you omit them, the default values will be used.

   c) To finish and give the final answer:
      {{
        "action": "final_answer",
        "content": "<final human-readable explanation, including any suggested commands>"
      }}

   - No code fences.
   - No additional keys.

3. KUBERNETES / K8S:
   - Prefer `kubectl` commands.
   - When namespaces matter, always include `-n <namespace>` in your commands.
   - For logs, prefer limited output.
   - If you don't know a value, propose discovery commands.

4. OCI / OKE:
   - You may also use `oci` CLI commands when needed.
   - Default region (if needed): {OCI_REGION}
   - Default compartment (if needed): {OCI_COMPARTMENT_OCID}

Remember: JSON ONLY, strictly following one of the allowed schemas.
"""
    return base_prompt


def _trim_message_history(state: AgentState, max_message_history: int = 8) -> None:
    if len(state["messages"]) > max_message_history + 1:
        state["messages"] = [state["messages"][0]] + state["messages"][-max_message_history:]


def _sanitize_model_output(raw_text: str) -> str:
    return raw_text.replace("```json", "").replace("```", "").strip()


# OpenAI/ChatGPT parsing remains strict, but Ollama/Qwen may wrap JSON with prose; this
# helper extracts a JSON object string suitable for json.loads when Ollama is detected.
def _prepare_json_for_loading(clean_text: str) -> str:
    if not is_ollama_backend():
        return clean_text

    try:
        json.loads(clean_text)
        return clean_text
    except json.JSONDecodeError:
        pass

    start = clean_text.find("{")
    if start != -1:
        for match in re.finditer(r"\}", clean_text[start:]):
            end = start + match.end()
            candidate = clean_text[start:end]
            try:
                json.loads(candidate)
                return candidate
            except json.JSONDecodeError:
                continue

    raise json.JSONDecodeError("Unable to decode JSON from model output", clean_text, 0)


def parse_llm_json(raw_output: str, provider: str, feature: str) -> dict:
    """Normalize and parse JSON returned by an LLM.

    Args:
        raw_output: Raw text returned by the model.
        provider: Provider name (e.g., "openai", "ollama").
        feature: Feature name for logging context (e.g., "night_mode", "sre_suggestions").

    Raises:
        ValueError: If the output is empty or cannot be parsed as JSON.
    """

    raw_output = raw_output or ""
    cleaned = raw_output.strip()
    if not cleaned:
        raise ValueError("Empty LLM output (nothing to parse)")

    if cleaned.startswith("```"):
        cleaned = cleaned[3:]
        if cleaned.lower().startswith("json"):
            cleaned = cleaned[4:]
        cleaned = cleaned.strip()
        if cleaned.endswith("```"):
            cleaned = cleaned[:-3]
        elif "```" in cleaned:
            cleaned = cleaned[: cleaned.rfind("```")]
        cleaned = cleaned.strip()

    try:
        json_payload = _prepare_json_for_loading(cleaned)
        return json.loads(json_payload)
    except Exception as exc:
        snippet = cleaned[:1000]
        print(
            "Failed to parse LLM output "
            f"(provider={provider}, feature={feature}): {exc}. Snippet: {snippet}"
        )
        raise ValueError(f"Unable to parse LLM output for {feature}: {exc}") from exc


def agent_engine_step(
    state: AgentState, user_decision: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    """Perform a single reasoning step without any I/O side effects."""

    if state["steps_done"] >= state["max_steps"]:
        return {"status": "done", "final_answer": "Reached max steps", "state": state}

    if OPENAI_API_KEY == "DUMMY_KEY_FOR_MOCK_DEMO":
        return {
            "status": "done",
            "final_answer": "Mock agent response.",
            "state": state,
        }

    llm_client = get_llm_client()
    if llm_client is None:
        return {
            "status": "done",
            "final_answer": "LLM client is not configured.",
            "state": state,
        }

    ran: Optional[Dict[str, Any]] = state.pop("pending_ran", None)

    if user_decision:
        decision_type = user_decision.get("type") or user_decision.get("decision")
        proposal = user_decision.get("proposal") or {}
        proposal_action = proposal.get("action") or user_decision.get("action")
        proposal_command = proposal.get("command") or user_decision.get("command") or ""

        if decision_type == "skip":
            skip_msg = "User skipped the remaining steps for this session."
            state["messages"].append({"role": "user", "content": skip_msg})
            return {"status": "done", "final_answer": skip_msg, "state": state}

        if decision_type == "approve" and proposal_action == "run_command" and proposal_command:
            result = run_shell_command(proposal_command)
            command_notes = _append_command_result_to_messages(
                state, proposal_command, result
            )
            ran = {
                "type": "command",
                "command": proposal_command,
                "result": result,
                "summary": command_notes.get("summary", ""),
                "highlights": command_notes.get("highlights", []),
            }
            state["pending_ran"] = ran
            running_message = proposal.get(
                "running_message",
                f"Executing command to {proposal.get('reason', 'proceed')}...",
            )
            return {
                "status": "running_command",
                "action": "run_command",
                "command": proposal_command,
                "running_message": running_message,
                "state": state,
            }
        elif decision_type == "approve" and proposal_action == "analyze_log":
            log_path = proposal.get("log_path") or ""
            digest = analyze_logs_locally(
                log_path,
                keywords=proposal.get("keywords"),
                context_lines=proposal.get("context_lines", 5),
                max_snippets=proposal.get("max_snippets", 50),
            )
            digest_for_model = truncate_for_model(digest, max_chars=8000)
            state["messages"].append(
                {"role": "user", "content": f"Local log analysis of {log_path}:\n{digest_for_model}"}
            )
            ran = {
                "type": "analyze_log",
                "log_path": log_path,
                "digest": digest,
            }
            state["pending_ran"] = ran
            return {
                "status": "running_command",
                "action": "analyze_log",
                "log_path": log_path,
                "running_message": f"Analyzing log at {log_path}...",
                "state": state,
            }
        elif decision_type == "deny" and proposal_action == "run_command" and proposal_command:
            deny_msg = f"The user denied running the command: {proposal_command}"
            state["messages"].append({"role": "user", "content": deny_msg})
        elif decision_type == "deny" and proposal_action == "analyze_log":
            log_path = proposal.get("log_path") or ""
            deny_msg = f"The user denied log analysis on: {log_path}"
            state["messages"].append({"role": "user", "content": deny_msg})
        elif user_decision.get("message"):
            state["messages"].append({"role": "user", "content": user_decision.get("message", "")})

    _trim_message_history(state)

    try:
        response = llm_client.responses.create(
            model=LLM_MODEL,
            input=state["messages"],
        )
    except Exception as exc:  # pragma: no cover - network
        return {"status": "error", "final_answer": f"Agent call failed: {exc}", "state": state}

    raw_text = response.output_text.strip() if hasattr(response, "output_text") else ""
    clean_text = _sanitize_model_output(raw_text)
    state["messages"].append({"role": "assistant", "content": clean_text})
    state["steps_done"] = state.get("steps_done", 0) + 1

    try:
        json_payload = _prepare_json_for_loading(clean_text)
        data = json.loads(json_payload)
    except json.JSONDecodeError:
        raw_msg = f"RAW MODEL OUTPUT (non-JSON): {clean_text}"
        notify_admin(raw_msg, "ERROR")
        return {"status": "done", "final_answer": raw_msg, "ran": ran, "state": state}

    action = data.get("action")
    if action == "final_answer":
        return {
            "status": "done",
            "final_answer": data.get("content", ""),
            "ran": ran,
            "state": state,
        }

    if action == "run_command":
        return {
            "status": "need_approval",
            "action": "run_command",
            "command": data.get("command", ""),
            "reason": data.get("reason", ""),
            "ran": ran,
            "state": state,
        }

    if action == "analyze_log":
        return {
            "status": "need_approval",
            "action": "analyze_log",
            "log_path": data.get("log_path", ""),
            "keywords": data.get("keywords"),
            "context_lines": data.get("context_lines", 5),
            "max_snippets": data.get("max_snippets", 50),
            "reason": data.get("reason", ""),
            "ran": ran,
            "state": state,
        }

    return {
        "status": "intermediate",
        "note": clean_text,
        "ran": ran,
        "state": state,
    }


def _extract_command_highlights(result: dict, max_lines: int = 4) -> List[str]:
    """Return a few key lines from stdout/stderr for UI-friendly display."""

    for key in ("stdout", "stderr"):
        content = result.get(key) or ""
        if not content:
            continue
        lines = [ln.strip() for ln in content.splitlines() if ln.strip()]
        if lines:
            return lines[:max_lines]
    return []


def _append_command_result_to_messages(state: AgentState, command: str, result: dict) -> Dict[str, Any]:
    stdout_trimmed = truncate_for_model(result.get("stdout", ""), max_chars=4000)
    stderr_trimmed = truncate_for_model(result.get("stderr", ""), max_chars=2000)
    exit_code = result.get("exit_code", 0)

    result_text = (
        f"Result of command: {command}\n"
        f"EXIT_CODE: {exit_code}\n"
        f"STDOUT:\n{stdout_trimmed}\n"
        f"STDERR:\n{stderr_trimmed}\n"
    )
    state["messages"].append({"role": "user", "content": result_text})

    highlights = _extract_command_highlights(result)
    headline = highlights[0] if highlights else ""
    summary = headline or f"Command `{command}` exited {exit_code}."
    if headline:
        summary = f"{headline} (exit {exit_code})."

    return {"summary": summary, "highlights": highlights}


def build_sre_agent_system_prompt(
    unified_context: Optional[Dict[str, Any]] = None,
    runtime_defaults: Optional[Dict[str, Any]] = None,
) -> str:
    """System prompt for the JSON-driven agent, with persona + context."""

    runtime_defaults = runtime_defaults or {}
    region = runtime_defaults.get("oci_region", OCI_REGION)
    compartment = runtime_defaults.get("oci_compartment_ocid", OCI_COMPARTMENT_OCID)
    kubeconfig = runtime_defaults.get("kubeconfig", KUBECONFIG)

    persona = build_sre_system_prompt(unified_context)
    context_summary = _summarize_unified_context_for_prompt(unified_context or {})

    return f"""
{persona}

You run with a command-and-log workflow:
- Propose kubectl/OCI/shell commands when they move the investigation forward.
- Request local log analysis for big files instead of reading them directly.
- Use outputs to decide next steps; finish with a short human-readable answer.
 - Use outputs to decide next steps; finish with a short human-readable answer.
- When you describe command_output, provide a one-sentence human summary plus a few key lines, never full logs.
- Final answers must recap what you checked, what you discovered, and the single next command/check.

Environment awareness:
- Default region: {region}
- Default compartment: {compartment}
- Kubeconfig: {kubeconfig}
- Night Mode monitors this cluster and feeds you recent events/reports.
- Cluster/Night Mode context (summary): {context_summary}

CRITICAL RULES:
1) NEVER claim you executed anything. The Python layer runs commands only after user approval.
2) ALWAYS respond in pure JSON (no markdown). Allowed shapes:
   a) Request a command:
      {{
        "action": "run_command",
        "command": "<exact shell command>",
        "reason": "<short explanation>"
      }}
   b) Request local log analysis (no shell command):
      {{
        "action": "analyze_log",
        "log_path": "<absolute path>",
        "keywords": ["ERROR", "Exception", "FATAL"],
        "context_lines": 5,
        "max_snippets": 50,
        "reason": "<short explanation>"
      }}
   c) Finish:
      {{
        "action": "final_answer",
        "content": "<final human-readable explanation, including any suggested commands>"
      }}
   - No extra keys. No code fences.

3) Kubernetes guidance:
   - Prefer `kubectl`; include `-n <namespace>` when needed.
   - Keep log outputs short (e.g., `kubectl logs <pod> -n <ns> --tail=100`).
   - If unsure of names, propose discovery commands.

4) OCI/OKE guidance:
   - You may use `oci` CLI when appropriate (defaults above).

5) Interaction strategy:
   - Favor a short chain of commands with clear reasons over massive outputs.
   - For destructive actions, be explicit in the reason and only propose them when requested.

Remember: JSON ONLY, using one of the allowed schemas.
"""


def build_cli_agent_system_prompt(
    context_data: Dict[str, Any], unified_context: Optional[Dict[str, Any]] = None
) -> str:
    runtime_defaults = {
        "oci_region": context_data.get("oci_region", OCI_REGION),
        "oci_compartment_ocid": context_data.get("oci_compartment_ocid", OCI_COMPARTMENT_OCID),
        "kubeconfig": context_data.get("kubeconfig", KUBECONFIG),
    }
    return build_sre_agent_system_prompt(unified_context, runtime_defaults)


def init_web_agent_state(user_question: str) -> AgentState:
    """
    Build the system+user messages for a web/HTTP session.
    Use build_unified_sre_context() and build_sre_agent_system_prompt(context) to create the system message.
    """

    unified = build_unified_sre_context()
    runtime_defaults = {
        "oci_region": OCI_REGION,
        "oci_compartment_ocid": OCI_COMPARTMENT_OCID,
        "kubeconfig": KUBECONFIG,
    }
    system_prompt = build_sre_agent_system_prompt(unified, runtime_defaults)
    return init_agent_state(system_prompt, user_question, max_steps=12)


def web_agent_step(
    state: AgentState, user_decision: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    """Thin wrapper around the shared agent engine for web usage."""

    return agent_engine_step(state, user_decision=user_decision)


def agent_session(initial_query: str, max_steps: int = 8):
    """
    Start an interaction session with Silky Sentinel for a single user query.
    The session may involve several 'run_command' / 'analyze_log' cycles + a final answer.
    """

    if OPENAI_API_KEY == "DUMMY_KEY_FOR_MOCK_DEMO":
        notify_admin(f"(MOCK) Received query: {initial_query}", "MOCK")
        print("MOCK MODE: no real reasoning, no commands executed.")
        return

    if client is None:
        raise RuntimeError("OpenAI client is not initialized. Check OPENAI_API_KEY in your .env.")

    notify_admin(f"User query: {initial_query}", "QUERY")

    unified_context = build_unified_sre_context()
    context_data = {
        "oci_region": OCI_REGION,
        "oci_compartment_ocid": OCI_COMPARTMENT_OCID,
        "kubeconfig": KUBECONFIG,
    }

    system_prompt = build_cli_agent_system_prompt(context_data, unified_context)
    state = init_agent_state(system_prompt, initial_query, max_steps=max_steps)
    pending_decision: Optional[Dict[str, Any]] = None

    while True:
        step_result = agent_engine_step(state, user_decision=pending_decision)
        pending_decision = None

        ran = step_result.get("ran")
        if ran and ran.get("type") == "command":
            result = ran.get("result", {})
            notify_admin(
                f"Command executed with exit_code={result.get('exit_code')}", "EXEC"
            )
            print("\n--- COMMAND RESULT ---")
            print(f"Exit code: {result.get('exit_code')}")
            print("STDOUT:")
            print(result.get("stdout") or "<empty>")
            print("\nSTDERR:")
            print(result.get("stderr") or "<empty>")
            print("-----------------------------------------\n")

        if ran and ran.get("type") == "analyze_log":
            print("\n--- LOCAL LOG ANALYSIS DIGEST ---")
            print(ran.get("digest", ""))
            print("-----------------------------------------\n")

        status = step_result.get("status")

        if status == "running_command":
            cmd = (
                step_result.get("command")
                or step_result.get("proposed_command")
                or (ran or {}).get("command", "")
            )
            running_message = step_result.get("running_message") or (
                f"Running {cmd}..." if cmd else "Running command..."
            )
            print(f"[EXEC] {running_message}")
            continue

        if status == "need_approval":
            action = step_result.get("action")
            if action == "run_command":
                command = step_result.get("command", "")
                reason = step_result.get("reason", "")

                notify_admin(f"Model requested command: {command}", "PLAN")

                print("\n--- COMMAND PROPOSED BY SILKY SENTINEL ---")
                if reason:
                    print(f"Reason: {reason}")
                print(f"Command:\n  {command}")
                choice = input(
                    "Approve and run this command? [y/n/s=skip session] "
                ).strip().lower()

                if choice == "s":
                    print("Skipping remaining steps for this session.")
                    return

                if choice != "y":
                    pending_decision = {
                        "type": "deny",
                        "proposal": step_result,
                    }
                    continue

                pending_decision = {
                    "type": "approve",
                    "proposal": step_result,
                }
                continue

            if action == "analyze_log":
                log_path = step_result.get("log_path", "")
                keywords = step_result.get("keywords") or None
                context_lines = int(step_result.get("context_lines", 5))
                max_snippets = int(step_result.get("max_snippets", 50))
                reason = step_result.get("reason", "")

                notify_admin(f"Model requested local log analysis on: {log_path}", "PLAN")

                print("\n--- LOCAL LOG ANALYSIS REQUESTED BY SILKY SENTINEL ---")
                if reason:
                    print(f"Reason: {reason}")
                print(f"Log path: {log_path}")
                print(f"Keywords: {keywords or '[default]'}")
                print(f"Context lines: {context_lines}, Max snippets: {max_snippets}")
                choice = input("Run local log analysis now? [y/n/s=skip session] ").strip().lower()

                if choice == "s":
                    print("Skipping remaining steps for this session.")
                    return

                if choice != "y":
                    pending_decision = {
                        "type": "deny",
                        "proposal": step_result,
                    }
                    continue

                pending_decision = {
                    "type": "approve",
                    "proposal": step_result,
                    "log_path": log_path,
                    "keywords": keywords,
                    "context_lines": context_lines,
                    "max_snippets": max_snippets,
                }
                continue

        if status == "done":
            final_answer = step_result.get("final_answer", "")
            notify_admin("Model provided final answer.", "PLAN")
            print("\n=== FINAL ANSWER ===\n")
            print(final_answer)
            print()
            return

        if status == "error":
            print(step_result.get("final_answer", "Agent error encountered."))
            return

        if status == "intermediate":
            continue

        if state["steps_done"] >= state["max_steps"]:
            notify_admin("Reached max_steps without final_answer.", "WARN")
            print("Stopped after reaching max_steps without a final answer.")
            return


# --------------------------------------------------------------------
# NIGHT MODE: autonomous nightly monitoring / analysis
# --------------------------------------------------------------------
def log_night_event(snapshot: dict, analysis: dict):
    entry = {
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "snapshot": snapshot,
        "analysis": analysis,
    }
    try:
        with open(NIGHT_LOG_PATH, "a") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception as e:
        print(f"Failed to write night mode log: {e}")


def _latest_night_severity(default: str = "UNKNOWN") -> str:
    if not NIGHT_LOG_PATH.exists():
        return default

    try:
        last_line = None
        with open(NIGHT_LOG_PATH, "r") as f:
            for line in f:
                if line.strip():
                    last_line = line

        if not last_line:
            return default

        data = json.loads(last_line)
        analysis = data.get("analysis", {}) if isinstance(data, dict) else {}
        severity = analysis.get("severity") or default
        return severity.upper()
    except Exception:
        return default


def night_collect_cluster_health() -> dict:
    snapshot = {
        "summary": {
            "cpu": {
                "total_cores": 0.0,
                "used_cores": 0.0,
                "utilization_percent": 0.0,
            },
            "memory": {
                "total_gib": 0.0,
                "used_gib": 0.0,
                "utilization_percent": 0.0,
            },
            "storage": {
                "total_gib": 0.0,
                "used_gib": 0.0,
                "utilization_percent": 0.0,
            },
            "pods": {
                "total": 0,
                "unhealthy": 0,
                "unhealthy_percent": 0.0,
                "by_status": {},
            },
            "nodes": {"count": 0, "ready": 0, "not_ready": 0},
            "alerts": {"last_severity": _latest_night_severity(), "open_incidents": 0},
            "queues": {
                "enabled": False,
                "total_backlog": 0,
                "top_queues": [],
            },
            "total_pods": 0,
            "bad_pods": 0,
        },
        "namespaces": {
            "top_by_cpu": [],
            "top_by_memory": [],
            "unhealthy_counts": [],
        },
        "pods": [],
        "errors": [],
    }

    def parse_cpu_quantity(value: str) -> float:
        if value is None:
            return 0.0
        try:
            if isinstance(value, (int, float)):
                return float(value)
            value = str(value).strip()
            if value.endswith("m"):
                return float(value[:-1]) / 1000
            return float(value)
        except Exception:
            return 0.0

    def parse_memory_to_gib(value: str) -> float:
        if value is None:
            return 0.0
        try:
            value = str(value).strip()
            multiplier = 1
            if value.lower().endswith("ki"):
                multiplier = 1 / (1024 * 1024)
                value = value[:-2]
            elif value.lower().endswith("mi"):
                multiplier = 1 / 1024
                value = value[:-2]
            elif value.lower().endswith("gi"):
                multiplier = 1
                value = value[:-2]
            elif value.lower().endswith("ti"):
                multiplier = 1024
                value = value[:-2]
            elif value.lower().endswith("k"):
                multiplier = 1 / (1024 * 1024)
                value = value[:-1]
            elif value.lower().endswith("m"):
                multiplier = 1 / 1024
                value = value[:-1]
            elif value.lower().endswith("g"):
                multiplier = 1
                value = value[:-1]
            return float(value) * multiplier
        except Exception:
            return 0.0

    # Pods
    pods_cmd = "kubectl get pods -A -o wide --no-headers"
    pods_result = run_shell_command(pods_cmd)

    if pods_result["exit_code"] != 0:
        snapshot["errors"].append(
            f"pods collector failed: {pods_result['stderr'] or 'unknown error'}"
        )
    else:
        lines = pods_result["stdout"].splitlines()
        status_counts = {}
        total = 0
        bad = 0

        for line in lines:
            parts = line.split()
            if len(parts) < 5:
                continue

            ns, name, ready, status, restarts = (
                parts[0],
                parts[1],
                parts[2],
                parts[3],
                parts[4],
            )

            age = parts[5] if len(parts) > 5 else ""
            node = parts[7] if len(parts) > 7 else None

            try:
                restarts = int(restarts)
            except Exception:
                restarts = -1

            pod = {
                "namespace": ns,
                "name": name,
                "status": status,
                "restarts": restarts,
                "age": age,
                "node": node,
                "reason": None,
            }
            snapshot["pods"].append(pod)

            total += 1
            status_counts[status] = status_counts.get(status, 0) + 1
            if status not in ("Running", "Completed") or restarts > 5:
                bad += 1

        bad_percent = (bad / total * 100) if total > 0 else 0.0
        snapshot["summary"]["pods"] = {
            "total": total,
            "unhealthy": bad,
            "unhealthy_percent": round(bad_percent, 2),
            "by_status": status_counts,
        }
        snapshot["summary"]["total_pods"] = total
        snapshot["summary"]["bad_pods"] = bad

    try:
        if NIGHT_LOG_PATH.exists():
            with open(NIGHT_LOG_PATH, "r") as f:
                recent = f.readlines()[-30:]
            incidents = 0
            for line in recent:
                try:
                    parsed = json.loads(line)
                    sev = (
                        parsed.get("analysis", {}).get("severity")
                        if isinstance(parsed, dict)
                        else None
                    )
                    if sev and str(sev).lower() in {"medium", "high", "critical"}:
                        incidents += 1
                except Exception:
                    continue
            snapshot["summary"]["alerts"]["open_incidents"] = incidents
    except Exception:
        pass

    # Node readiness counts
    nodes_cmd = "kubectl get nodes --no-headers"
    nodes_result = run_shell_command(nodes_cmd)
    if nodes_result["exit_code"] != 0:
        snapshot["errors"].append(
            f"nodes collector failed: {nodes_result['stderr'] or 'unknown error'}"
        )
    else:
        ready = 0
        not_ready = 0
        for line in nodes_result["stdout"].splitlines():
            parts = line.split()
            if len(parts) < 2:
                continue
            status = parts[1]
            if status.startswith("Ready"):
                ready += 1
            else:
                not_ready += 1

        snapshot["summary"]["nodes"] = {
            "count": ready + not_ready,
            "ready": ready,
            "not_ready": not_ready,
        }

    # Capacity from node JSON
    nodes_json_cmd = "kubectl get nodes -o json"
    nodes_json_result = run_shell_command(nodes_json_cmd)
    cpu_total = 0.0
    mem_total_gib = 0.0
    if nodes_json_result["exit_code"] != 0:
        snapshot["errors"].append(
            f"nodes_json collector failed: {nodes_json_result['stderr'] or 'unknown error'}"
        )
    else:
        try:
            data = json.loads(nodes_json_result["stdout"] or "{}")
            items = data.get("items", [])
            for item in items:
                capacity = (
                    item.get("status", {}).get("capacity")
                    if isinstance(item, dict)
                    else {}
                )
                cpu_total += parse_cpu_quantity(capacity.get("cpu"))
                mem_total_gib += parse_memory_to_gib(capacity.get("memory"))
        except Exception as exc:
            snapshot["errors"].append(f"nodes_json parse error: {exc}")

    # Utilization from kubectl top
    top_nodes_cmd = "kubectl top nodes"
    top_nodes_result = run_shell_command(top_nodes_cmd)
    cpu_used = 0.0
    mem_used_gib = 0.0
    if top_nodes_result["exit_code"] != 0:
        snapshot["errors"].append(
            f"top_nodes collector failed: {top_nodes_result['stderr'] or 'unknown error'}"
        )
    else:
        lines = top_nodes_result["stdout"].splitlines()
        for line in lines[1:]:  # skip header
            parts = line.split()
            if len(parts) < 4:
                continue
            cpu_used += parse_cpu_quantity(parts[1])
            mem_used_gib += parse_memory_to_gib(parts[3])

    cpu_util = (cpu_used / cpu_total * 100) if cpu_total > 0 else 0.0
    mem_util = (mem_used_gib / mem_total_gib * 100) if mem_total_gib > 0 else 0.0

    snapshot["summary"]["cpu"] = {
        "total_cores": round(cpu_total, 2),
        "used_cores": round(cpu_used, 2),
        "utilization_percent": round(cpu_util, 2),
    }

    snapshot["summary"]["memory"] = {
        "total_gib": round(mem_total_gib, 2),
        "used_gib": round(mem_used_gib, 2),
        "utilization_percent": round(mem_util, 2),
    }

    # Namespace leaderboards from kubectl top pods
    top_pods_cmd = "kubectl top pods -A --no-headers"
    top_pods_result = run_shell_command(top_pods_cmd)
    ns_cpu = {}
    ns_mem = {}
    if top_pods_result["exit_code"] != 0:
        snapshot["errors"].append(
            f"top_pods collector failed: {top_pods_result['stderr'] or 'unknown error'}"
        )
    else:
        for line in top_pods_result["stdout"].splitlines():
            parts = line.split()
            if len(parts) < 4:
                continue
            ns, _, cpu_raw, mem_raw = parts[0], parts[1], parts[2], parts[3]
            cpu_mcores = round(parse_cpu_quantity(cpu_raw) * 1000, 2)
            mem_mib = round(parse_memory_to_gib(mem_raw) * 1024, 2)
            ns_cpu[ns] = ns_cpu.get(ns, 0) + cpu_mcores
            ns_mem[ns] = ns_mem.get(ns, 0) + mem_mib

    top_cpu = sorted(ns_cpu.items(), key=lambda kv: kv[1], reverse=True)[:5]
    top_mem = sorted(ns_mem.items(), key=lambda kv: kv[1], reverse=True)[:5]

    pods_per_namespace = {}
    for pod in snapshot["pods"]:
        pods_per_namespace[pod["namespace"]] = pods_per_namespace.get(
            pod["namespace"], 0
        ) + 1

    snapshot["namespaces"]["top_by_cpu"] = [
        {
            "namespace": ns,
            "cpu_mcores": cpu,
            "pods": pods_per_namespace.get(ns, 0),
        }
        for ns, cpu in top_cpu
    ]
    snapshot["namespaces"]["top_by_memory"] = [
        {
            "namespace": ns,
            "memory_mib": mem,
            "pods": pods_per_namespace.get(ns, 0),
        }
        for ns, mem in top_mem
    ]

    unhealthy_by_namespace = {}
    for pod in snapshot["pods"]:
        if pod["status"] not in ("Running", "Completed") or pod["restarts"] > 5:
            unhealthy_by_namespace[pod["namespace"]] = (
                unhealthy_by_namespace.get(pod["namespace"], 0) + 1
            )

    snapshot["namespaces"]["unhealthy_counts"] = [
        {"namespace": ns, "unhealthy_pods": count}
        for ns, count in sorted(
            unhealthy_by_namespace.items(), key=lambda kv: kv[1], reverse=True
        )
    ]

    # Storage snapshot (local disk approximation)
    try:
        usage = shutil.disk_usage("/")
        total_gib = usage.total / (1024**3)
        used_gib = usage.used / (1024**3)
        storage_util = (used_gib / total_gib * 100) if total_gib else 0.0
        snapshot["summary"]["storage"] = {
            "total_gib": round(total_gib, 2),
            "used_gib": round(used_gib, 2),
            "utilization_percent": round(storage_util, 2),
        }
    except Exception as exc:
        snapshot["errors"].append(f"storage collector failed: {exc}")

    return snapshot


def summarize_night_mode(events: list, latest_report: str | None) -> dict:
    """
    Use OpenAI (LLM_MODEL) to produce:
    - summary_markdown: human-readable summary of events
    - severity_histogram: counts of severities found
    - recommendations: short bullet tips
    All fields returned as a dict. Input must be truncated using existing helpers.
    """

    safe_events = events if isinstance(events, list) else []

    histogram = {}
    for ev in safe_events:
        severity = None
        if isinstance(ev, dict):
            analysis = ev.get("analysis") or {}
            severity = analysis.get("severity") or ev.get("severity")
        if severity:
            sev_norm = str(severity).lower()
            histogram[sev_norm] = histogram.get(sev_norm, 0) + 1

    if client is None:
        latest_sev = next(iter(histogram)) if histogram else "unknown"
        return {
            "summary_markdown": (
                "LLM client is not configured. "
                f"Observed {len(safe_events)} event(s); latest severity={latest_sev}."
            ),
            "severity_histogram": histogram,
            "recommendations": [
                "Configure OPENAI_API_KEY to enable rich Night Mode summaries.",
                "Review night_mode_events.log for detailed diagnostics.",
            ],
        }

    events_json = truncate_for_model(json.dumps(safe_events[-120:]), max_chars=8000)
    report_text = truncate_for_model(latest_report or "", max_chars=4000)

    system = (
        "You are Silky Sentinel. Summarize Night Mode activity for SREs.\n"
        "Respond strictly in JSON with keys: summary_markdown (markdown string), "
        "severity_histogram (object of severity->count), recommendations (array of short strings)."
    )

    user = (
        "Recent Night Mode events (truncated):\n"
        f"{events_json}\n\n"
        "Latest Night Mode report (truncated; may be empty):\n"
        f"{report_text}"
    )

    try:
        resp = client.responses.create(
            model=LLM_MODEL,
            input=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        )

        raw = resp.output_text or ""
        parsed = parse_llm_json(raw, LLM_PROVIDER, "night_mode_summary")

        return {
            "summary_markdown": parsed.get("summary_markdown") or "(No summary returned)",
            "severity_histogram": parsed.get("severity_histogram") or histogram,
            "recommendations": parsed.get("recommendations") or [],
        }
    except Exception:
        return {
            "summary_markdown": (
                "Failed to generate Night Mode summary. "
                "Check OpenAI configuration or logs for details."
            ),
            "severity_histogram": histogram,
            "recommendations": [
                "Verify OPENAI_API_KEY is set and valid.",
                "Inspect recent Night Mode events for anomalies.",
            ],
        }


def night_analyze_with_llm(snapshot: dict) -> dict:
    if client is None:
        return {
            "severity": "info",
            "title": "LLM disabled",
            "summary": "OpenAI client not configured.",
            "notable_pods": [],
            "recommendations": [],
        }

    system = (
        "You are Silky Sentinel Night Mode — an autonomous SRE guard.\n"
        "You receive cluster snapshots and must classify anomalies.\n"
        "Respond in STRICT JSON:\n"
        "{\n"
        '  "severity": "ok" | "low" | "medium" | "high" | "critical",\n'
        '  "title": "<short title>",\n'
        '  "summary": "<2-3 sentences>",\n'
        '  "notable_pods": [ {"namespace": "...", "name": "...", "status": "...", "restarts": <int>} ],\n'
        '  "recommendations": ["string", "string"]\n'
        "}"
    )

    user = json.dumps(snapshot)

    try:
        resp = client.responses.create(
            model=LLM_MODEL,
            input=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        )

        raw = resp.output_text or ""
        parsed = parse_llm_json(raw, LLM_PROVIDER, "night_mode")
        return parsed
    except ValueError as e:
        raw = resp.output_text or ""
        snippet = (raw.strip() or "(empty output)")[:400]
        print(
            "Using Night Mode fallback due to parse error "
            f"(provider={LLM_PROVIDER}, feature=night_mode): {e}. Snippet: {snippet}"
        )
        return {
            "severity": "unknown",
            "title": "LLM returned unstructured output",
            "summary": f"LLM returned unstructured output. Raw snippet: {snippet}",
            "notable_pods": [],
            "recommendations": [],
        }
    except Exception as e:
        return {
            "severity": "unknown",
            "title": "Night Mode LLM call failed",
            "summary": (f"Error during LLM analysis: {e}"),
            "notable_pods": [],
            "recommendations": [
                "Verify connectivity to the LLM endpoint.",
                "Consider increasing LLM_TIMEOUT_SECONDS if timeouts persist.",
            ],
        }


def generate_night_mode_report() -> tuple[str, str]:
    """
    Read night_mode_events.log, aggregate everything, and produce
    a human-readable report string + save it to a timestamped file.

    Returns (report_text, report_file_path_str).
    """
    if not NIGHT_LOG_PATH.exists():
        report = "No Night Mode events found. night_mode_events.log does not exist yet."
        return report, str(NIGHT_LOG_PATH)

    events = []
    with open(NIGHT_LOG_PATH, "r") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue

    if not events:
        report = "Night Mode log exists but contains no valid events."
        return report, str(NIGHT_LOG_PATH)

    # Basic stats
    first_ts = events[0].get("timestamp")
    last_ts = events[-1].get("timestamp")
    total_cycles = len(events)

    # Severity distribution
    severity_counts = {}
    notable_pods_index = {}  # key: (ns, name) -> latest info

    for ev in events:
        analysis = ev.get("analysis", {})
        sev = analysis.get("severity", "unknown")
        severity_counts[sev] = severity_counts.get(sev, 0) + 1

        for p in analysis.get("notable_pods", []):
            key = (p.get("namespace"), p.get("name"))
            notable_pods_index[key] = {
                "namespace": p.get("namespace"),
                "name": p.get("name"),
                "status": p.get("status"),
                "restarts": p.get("restarts"),
            }

    last_snapshot = events[-1].get("snapshot", {})
    last_summary = last_snapshot.get("summary", {})
    last_analysis = events[-1].get("analysis", {})

    # Build human-readable text
    lines = []
    lines.append("🌙 Silky Sentinel — Night Mode Final Report")
    lines.append("=" * 60)
    lines.append(f"Monitoring window : {first_ts}  →  {last_ts}")
    lines.append(f"Total check cycles: {total_cycles}")
    lines.append("")

    lines.append("Severity distribution over the session:")
    for sev, count in sorted(severity_counts.items()):
        lines.append(f"  - {sev}: {count} cycle(s)")
    lines.append("")

    pods_summary = last_summary.get("pods", {}) if isinstance(last_summary, dict) else {}
    lines.append("Last snapshot cluster summary:")
    lines.append(f"  - Total pods       : {pods_summary.get('total', 'N/A')}")
    lines.append(f"  - Unhealthy pods   : {pods_summary.get('unhealthy', 'N/A')}")
    lines.append(
        f"  - Unhealthy percent: {pods_summary.get('unhealthy_percent', 'N/A')}%"
    )
    lines.append("")

    lines.append("Last analysis from Night Mode:")
    lines.append(f"  - Severity: {last_analysis.get('severity', 'unknown')}")
    lines.append(f"  - Title   : {last_analysis.get('title', 'N/A')}")
    summary = last_analysis.get("summary") or ""
    if summary:
        lines.append("  - Summary :")
        for line in summary.splitlines():
            lines.append(f"      {line}")
    lines.append("")

    if notable_pods_index:
        lines.append("Most frequently mentioned notable pods:")
        for (ns, name), info in sorted(notable_pods_index.items()):
            lines.append(
                f"  - {ns}/{name}: status={info.get('status')}, restarts={info.get('restarts')}"
            )
    else:
        lines.append("No notable pods were flagged across the session.")
    lines.append("")

    lines.append("What Night Mode actually did under the hood:")
    lines.append("  - On each cycle, it executed:")
    lines.append("      kubectl get pods -A --no-headers")
    lines.append("    This command lists all pods in all namespaces with their READY, STATUS, and RESTARTS.")
    lines.append("    Night Mode parses this into a structured snapshot, sends it to the LLM to assess")
    lines.append("    anomalies (restart spikes, non-Running statuses, critical workloads), and logs both")
    lines.append("    the raw snapshot and the AI's analysis to night_mode_events.log.")
    lines.append("")
    lines.append("  - Every command that Night Mode (and chat mode) runs is also recorded in audit.log,")
    lines.append("    with timestamp, command string, exit code, and small stdout/stderr snippets for")
    lines.append("    later auditing and incident review.")
    lines.append("")

    report_text = "\n".join(lines)

    # Save to reports/report_YYYYmmdd_HHMMSS.txt
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    report_file = REPORTS_DIR / f"night_mode_report_{stamp}.txt"
    try:
        with open(report_file, "w") as f:
            f.write(report_text)
    except Exception as e:
        print(f"Failed to write Night Mode report file: {e}")

    return report_text, str(report_file)


def generate_sre_suggestions(
    snapshot: dict, events: list, latest_report: str | None
) -> dict:
    """
    Use the existing OpenAI client (LLM_MODEL) via Responses API to generate a small set of SRE suggestions.

    Input:
      - snapshot: the cluster metrics summary returned by night_collect_cluster_health().
      - events: recent Night Mode events (already have helpers to read them).
      - latest_report: text of the most recent Night Mode report, or "".

    Output:
      - A dict with:
        {
          "suggestions": [
            {
              "id": "sug-0001",
              "title": "Scale workers in prod",
              "reason": "Queue backlog in prod exceeded 10k messages for 15 minutes.",
              "action": "Scale the 'iacc-worker' deployment in 'prod' from 4 to 6 replicas.",
              "command": "kubectl scale deployment iacc-worker -n prod --replicas=6",
              "risk": "medium",   # low | medium | high
              "category": "capacity" # capacity | reliability | pods | nodes | queues | networking
            },
            ...
          ]
        }

    The model MUST respond with valid JSON exactly in this shape (no prose).
    """

    if client is None:
        return {"suggestions": []}

    context = {
        "snapshot": snapshot,
        "events": events,
        "latest_report": latest_report,
    }
    system_prompt = (
        build_sre_system_prompt(context)
        + "You receive Kubernetes metrics, recent Night Mode events, and a high level report. "
        "You must respond ONLY with JSON under a 'suggestions' key. Each suggestion must have a "
        "concrete kubectl or diagnostic command in 'command', and a short 'action' sentence describing "
        "what will be done."
    )

    snapshot_block = truncate_for_model(json.dumps(snapshot or {}))
    events_block = truncate_for_model(json.dumps(events or []))
    latest_report_block = truncate_for_model(latest_report or "")

    user_prompt = (
        "Cluster snapshot:\n"
        f"{snapshot_block}\n\n"
        "Recent events:\n"
        f"{events_block}\n\n"
        "Latest Night Mode report (text):\n"
        f"{latest_report_block}"
    )

    try:
        response = client.responses.create(
            model=LLM_MODEL,
            input=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        )

        raw_text = response.output_text or ""
        data = parse_llm_json(raw_text, LLM_PROVIDER, "sre_suggestions")
        if not isinstance(data, dict):
            return {"suggestions": []}
        suggestions = data.get("suggestions")
        if not isinstance(suggestions, list):
            return {"suggestions": []}
        return {"suggestions": suggestions}
    except ValueError as exc:
        snippet_lines = [line.strip() for line in (response.output_text or "").splitlines() if line.strip()]
        if not snippet_lines:
            snippet_lines = [(response.output_text or "(empty output)").strip() or "(empty output)"]

        print(
            "Using SRE suggestions fallback due to parse error "
            f"(provider={LLM_PROVIDER}, feature=sre_suggestions): {exc}."
        )

        fallback_suggestions = []
        for idx, line in enumerate(snippet_lines, start=1):
            fallback_suggestions.append(
                {
                    "id": f"sug-fallback-{idx:04d}",
                    "title": line[:80],
                    "reason": "LLM returned unstructured output.",
                    "action": line,
                    "command": "",
                    "risk": "unknown",
                    "category": "general",
                }
            )

        return {"suggestions": fallback_suggestions}
    except Exception:
        return {"suggestions": []}


def night_mode_loop(interval_seconds: int = 300, stop_event=None):
    """
    Night Mode:
    - Every interval, collect snapshot
    - Let LLM judge & summarize
    - Log everything
    - Send Slack alerts on medium+ severity or when state changes
    - On Ctrl+C, generate a final human-readable report and save it.
    """
    notify_admin(f"Starting Silky Sentinel Night Mode (every {interval_seconds}s).", "INFO")
    last_fingerprint = None

    interrupted = False

    try:
        while True:
            snapshot = night_collect_cluster_health()
            analysis = night_analyze_with_llm(snapshot)

            # Save to night_mode_events.log
            log_night_event(snapshot, analysis)

            # Build fingerprint to avoid spamming same issue repeatedly
            try:
                issues = [
                    {
                        "ns": p["namespace"],
                        "name": p["name"],
                        "status": p["status"],
                        "restarts": p["restarts"],
                    }
                    for p in snapshot.get("pods", [])
                    if p["status"] not in ("Running", "Completed") or p["restarts"] > 5
                ]
                fingerprint = json.dumps(issues, sort_keys=True)
            except Exception:
                fingerprint = json.dumps(snapshot.get("summary", {}), sort_keys=True)

            if fingerprint != last_fingerprint:
                sev = analysis.get("severity", "unknown")
                title = analysis.get("title", "Cluster report")
                summary = analysis.get("summary", "")

                if sev in ("medium", "high", "critical"):
                    notify_admin(f"[{sev.upper()}] {title}\n\n{summary}", "ALERT")
                else:
                    notify_admin(f"[{sev.upper()}] {title}\n\n{summary}", "INFO")

                last_fingerprint = fingerprint

            if stop_event:
                # Wait for the interval, but exit early if stop requested
                if stop_event.wait(interval_seconds):
                    interrupted = True
                    break
            else:
                time.sleep(interval_seconds)

    except KeyboardInterrupt:
        interrupted = True

    if interrupted:
        # User hit Ctrl+C or server requested stop: generate final session report
        print("\n🌙 Night Mode interrupted. Generating final report...\n")
        report_text, report_file = generate_night_mode_report()
        print(report_text)
        print(f"\n📄 Night Mode report saved to: {report_file}")
        notify_admin(f"Night Mode stopped; report saved to {report_file}", "INFO")


# --------------------------------------------------------------------
# CLI / REPL ENTRYPOINT
# --------------------------------------------------------------------
def main():
    try:
        kube_path = ensure_kubeconfig()
        print(f"✔ Using kubeconfig: {kube_path}")
    except Exception as e:
        print(f"❌ Kubeconfig error: {e}")
        return

    mode = SILKY_MODE.lower()

    # 🌙 --- NIGHT MODE ---
    if mode == "night":
        print("🌙 Silky Sentinel — NIGHT MODE (Autonomous Monitoring)")
        print(f"Logging to: {NIGHT_LOG_PATH}")
        night_mode_loop(interval_seconds=NIGHT_INTERVAL_SECONDS)
        return

    # 🤖 --- CHAT MODE (existing) ---
    print("🤖 Silky Sentinel — K8s/OCI Agent (with optional command execution & log analysis)")
    print("Type a question or request, or 'exit' / 'quit' to leave.")
    print()
    print("Examples:")
    print("  - I have domain iportal.dev.silkysystems.com, find the pods and show me how to get their logs.")
    print("  - Analyze the full logs for the ingress controller at /var/log/nginx/ingress.log.")
    print("  - Show me how to inspect restarts for pods related to iportal in any namespace.")
    print("  - Scale a deployment in namespace prod from 2 to 4 replicas (just show commands).")
    print()

    while True:
        try:
            query = input("Silky Sentinel> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nExiting Silky Sentinel.")
            break

        if not query:
            continue

        if query.lower() in ("exit", "quit"):
            print("Goodbye 👋")
            break

        agent_session(query)


# --------------------------------------------------------------------
# SCRIPT ENTRYPOINT
# --------------------------------------------------------------------
if __name__ == "__main__":
    main()
