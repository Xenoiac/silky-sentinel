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
LLM_MODEL = os.getenv("LLM_MODEL", "gpt-5.1")  # default model

# Mode & night-mode config
SILKY_MODE = os.getenv("SILKY_MODE", "chat")
NIGHT_INTERVAL_SECONDS = int(os.getenv("NIGHT_INTERVAL_SECONDS", "300"))
NIGHT_LOG_PATH = Path(__file__).resolve().parent / "night_mode_events.log"
REPORTS_DIR = Path(__file__).resolve().parent / "reports"
AUDIT_LOG_PATH = Path(__file__).resolve().parent / "audit.log"

# --------------------------------------------------------------------
# OpenAI Client
# --------------------------------------------------------------------
client = None
if OPENAI_API_KEY != "DUMMY_KEY_FOR_MOCK_DEMO":
    if not OPENAI_API_KEY:
        print("Error: OPENAI_API_KEY not found or invalid. Please update your .env file.")
        sys.exit(1)
    client = OpenAI(api_key=OPENAI_API_KEY)


# --------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------
def truncate_for_model(text: str, max_chars: int = 4000) -> str:
    """Trim long text so we don't blow the model context window."""
    if text is None:
        return ""
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + f"\n\n...[truncated, original length {len(text)} chars]..."


def build_sre_system_prompt(context: Optional[Dict[str, Any]] = None) -> str:
    """
    Build a system prompt for Silky Sentinel's SRE brain.

    The assistant should:
    - Behave like a senior SRE / DevOps engineer for a Kubernetes platform.
    - Be cluster-aware and cost-aware (reliability, latency, resource use, and cost).
    - Answer concisely in 1–4 short sentences.
    - Explain issues in language understandable by SREs, developers, and managers.
    - When possible, mention: what was checked, what it means, and the single most important next step.
    """
    base = (
        "You are Silky Sentinel, a senior Site Reliability Engineer for a Kubernetes platform. "
        "You always answer concisely (1-4 short sentences) and clearly, in language that SREs, developers, and managers can all understand. "
        "You are cluster-aware and cost-aware: you care about reliability, latency, resource utilization, and cloud cost. "
        "When you describe a result, mention what you checked, what it means, and the single most important next step, if any. "
    )
    if context:
        # Provide a compact, safe view of context (do not dump huge blobs).
        base += f"Context summary: {str(context)[:1200]} "
    return base


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


def apply_sre_suggestion(suggestion: dict) -> dict:
    """
    Execute a single actionable suggestion and summarize the result.

    suggestion includes:
      - title
      - reason
      - action (human sentence)
      - command (string; a kubectl or diagnostic command)
    """

    command = suggestion.get("command", "")
    result = run_shell_command(command, timeout=60, cwd=None)

    summary_prompt = f"""
You are summarizing the outcome of an SRE automation action. Provide a concise status update (1-3 sentences) for leadership.

Suggestion title: {suggestion.get('title','')}
Reason: {suggestion.get('reason','')}
Proposed action: {suggestion.get('action','')}
Command executed: {command}

Command exit code: {result.get('exit_code')}
STDOUT (truncated): {truncate_for_model(result.get('stdout'), max_chars=1200)}
STDERR (truncated): {truncate_for_model(result.get('stderr'), max_chars=800)}

Respond with a clean, high-level summary noting success/failure and key findings. Avoid raw logs.
"""

    summary_text = ""
    status = "ok"

    if OPENAI_API_KEY == "DUMMY_KEY_FOR_MOCK_DEMO" or client is None:
        status = "error" if result.get("exit_code", 1) != 0 else "ok"
        summary_text = (
            "Mock execution; review audit logs for details." if client is None else summary_text
        )
    else:
        try:
            resp = client.responses.create(
                model=LLM_MODEL,
                input=[
                    {
                        "role": "user",
                        "content": summary_prompt,
                    }
                ],
            )
            summary_text = resp.output_text if hasattr(resp, "output_text") else ""
        except Exception as exc:  # pragma: no cover - network
            summary_text = f"Failed to summarize result: {exc}"
            status = "error"

    return {
        "status": status,
        "summary": summary_text,
        "exit_code": result.get("exit_code"),
        "command": command,
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


def agent_step(state: AgentState, user_decision: Optional[dict] = None) -> Dict[str, Any]:
    if state["steps_done"] >= state["max_steps"]:
        return {"status": "done", "answer": "Reached max steps", "state": state}

    if user_decision and "message" in user_decision:
        state["messages"].append({"role": "user", "content": user_decision.get("message", "")})

    if OPENAI_API_KEY == "DUMMY_KEY_FOR_MOCK_DEMO":
        return {
            "status": "done",
            "answer": "Mock agent response.",
            "state": state,
        }

    try:
        resp = client.responses.create(
            model=LLM_MODEL,
            input=state["messages"],
        )
    except Exception as exc:  # pragma: no cover - network
        return {"status": "error", "answer": f"Agent call failed: {exc}"}

    output_text = resp.output_text if hasattr(resp, "output_text") else ""
    state["messages"].append({"role": "assistant", "content": output_text})
    state["steps_done"] += 1

    try:
        parsed = json.loads(output_text)
    except json.JSONDecodeError:
        return {"status": "done", "answer": output_text, "state": state}

    action = parsed.get("action")
    if action == "final_answer":
        return {"status": "done", "answer": parsed.get("content", ""), "state": state}

    if action == "run_command":
        reason = parsed.get("reason", "")
        command = parsed.get("command", "")
        return {
            "status": "need_approval",
            "action": "run_command",
            "command": command,
            "reason": reason,
            "state": state,
        }

    if action == "analyze_log":
        digest = analyze_logs_locally(
            parsed.get("log_path", ""),
            keywords=parsed.get("keywords"),
            context_lines=parsed.get("context_lines", 5),
            max_snippets=parsed.get("max_snippets", 50),
        )
        state["messages"].append({"role": "system", "content": digest})
        return agent_step(state)

    return {"status": "intermediate", "answer": output_text, "state": state}


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

    context_data = {
        "oci_region": OCI_REGION,
        "oci_compartment_ocid": OCI_COMPARTMENT_OCID,
        "kubeconfig": KUBECONFIG,
    }

    system_prompt = f"""
You are 'Silky Sentinel', a senior DevOps/SRE assistant for Silky Systems.

You are running in a special mode where you can:
- Propose concrete shell commands (kubectl, oci, bash, etc.).
- Request local log analysis on large files.
- Receive the actual outputs/digests back.
- Use that data to decide next steps.
- Eventually provide a final human-readable answer.

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
      - Use this for HUGE logs (tens of MB). Do NOT ask to run shell commands
        that dump massive logs and then send all stdout to the model.

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
   - For logs, prefer limited output, e.g.:
       - `kubectl logs <pod> -n <ns> --tail=100`
   - If you only know a domain/host (Ingress), you may:
       - `kubectl get ingress -A | grep <domain>`
       - `kubectl describe ingress -n <ns> <name>`
       - then find Service -> Deployment -> Pods -> `kubectl logs ...`.
   - If you don't know a value (namespace, pod name, etc.), propose discovery commands.

4. OCI / OKE:
   - You may also use `oci` CLI commands when needed.
   - Default region (if needed): {context_data['oci_region']}
   - Default compartment (if needed): {context_data['oci_compartment_ocid']}

5. TOOL RESULTS:
   - After a command is run, you will receive a message containing:
       - The command
       - exit code
       - stdout
       - stderr
   - After a log analysis is run, you will receive a digest summary.
   - Use that data to decide the next command or to produce the final answer.

6. INTERACTION STRATEGY:
   - Prefer a **short chain of commands** + occasional log analysis over huge raw outputs.
   - For destructive operations (delete, scale down, restart, etc.), you should:
       - Clearly reflect that in your 'reason'.
       - Only propose them if the user explicitly requested it.

Remember: JSON ONLY, strictly following one of the allowed schemas.
"""

    # Conversation messages for Responses API
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": initial_query},
    ]

    for step in range(max_steps):
        # Limit history to avoid context explosion (system + last N messages)
        max_message_history = 8
        if len(messages) > max_message_history + 1:
            messages = [messages[0]] + messages[-max_message_history:]

        response = client.responses.create(
            model=LLM_MODEL,
            input=messages,
        )

        raw_text = response.output_text.strip()
        # Handle accidental ```json ... ``` wrappers
        clean_text = raw_text.replace("```json", "").replace("```", "").strip()

        try:
            data = json.loads(clean_text)
        except json.JSONDecodeError:
            notify_admin("Model returned non-JSON response, stopping.", "ERROR")
            print("RAW MODEL OUTPUT:\n", raw_text)
            return

        action = data.get("action")

        # ------------------ run_command ------------------
        if action == "run_command":
            command = data.get("command", "")
            reason = data.get("reason", "")

            notify_admin(f"Model requested command: {command}", "PLAN")

            print("\n--- COMMAND PROPOSED BY SILKY SENTINEL ---")
            if reason:
                print(f"Reason: {reason}")
            print(f"Command:\n  {command}")
            choice = input("Approve and run this command? [y/n/s=skip session] ").strip().lower()

            if choice == "s":
                print("Skipping remaining steps for this session.")
                return

            if choice != "y":
                deny_msg = f"The user denied running the command: {command}"
                messages.append({"role": "user", "content": deny_msg})
                continue

            # Run the command
            result = run_shell_command(command)
            notify_admin(
                f"Command executed with exit_code={result['exit_code']}", "EXEC"
            )

            # Show to user
            print("\n--- COMMAND RESULT ---")
            print(f"Exit code: {result['exit_code']}")
            print("STDOUT:")
            print(result["stdout"] or "<empty>")
            print("\nSTDERR:")
            print(result["stderr"] or "<empty>")
            print("-----------------------------------------\n")

            # Feed result back to the model (trimmed)
            stdout_trimmed = truncate_for_model(result["stdout"], max_chars=4000)
            stderr_trimmed = truncate_for_model(result["stderr"], max_chars=2000)

            result_text = (
                f"Result of command: {command}\n"
                f"EXIT_CODE: {result['exit_code']}\n"
                f"STDOUT:\n{stdout_trimmed}\n"
                f"STDERR:\n{stderr_trimmed}\n"
            )
            messages.append({"role": "user", "content": result_text})
            continue

        # ------------------ analyze_log ------------------
        elif action == "analyze_log":
            log_path = data.get("log_path", "")
            keywords = data.get("keywords") or None
            context_lines = int(data.get("context_lines", 5))
            max_snippets = int(data.get("max_snippets", 50))
            reason = data.get("reason", "")

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
                deny_msg = f"The user denied log analysis on: {log_path}"
                messages.append({"role": "user", "content": deny_msg})
                continue

            # Run local analysis
            digest = analyze_logs_locally(
                log_path=log_path,
                keywords=keywords,
                context_lines=context_lines,
                max_snippets=max_snippets,
            )

            # Show digest to user
            print("\n--- LOCAL LOG ANALYSIS DIGEST ---")
            print(digest)
            print("-----------------------------------------\n")

            # Feed digest back into the model
            digest_for_model = truncate_for_model(digest, max_chars=8000)
            messages.append({
                "role": "user",
                "content": f"Local log analysis of {log_path}:\n{digest_for_model}"
            })
            continue

        # ------------------ final_answer ------------------
        elif action == "final_answer":
            content = data.get("content", "")
            notify_admin("Model provided final answer.", "PLAN")
            print("\n=== FINAL ANSWER ===\n")
            print(content)
            print()
            return

        else:
            notify_admin(f"Unknown action returned: {action}", "ERROR")
            print("Unknown action in model response:\n", data)
            return

    # If we exit the loop without final_answer
    notify_admin("Reached max_steps without final_answer.", "WARN")
    print("Stopped after reaching max_steps without a final answer.")


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

        raw = resp.output_text.strip()
        raw = raw.replace("```json", "").replace("```", "").strip()
        parsed = json.loads(raw)

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

    resp = client.responses.create(
        model=LLM_MODEL,
        input=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    )

    raw = resp.output_text.strip()
    raw = raw.replace("```json", "").replace("```", "").strip()

    try:
        return json.loads(raw)
    except:
        return {
            "severity": "unknown",
            "title": "Failed to parse LLM output",
            "summary": raw[:500],
            "notable_pods": [],
            "recommendations": [],
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

        raw_text = response.output_text.strip()
        clean_text = raw_text.replace("```json", "").replace("```", "").strip()
        data = json.loads(clean_text)
        if not isinstance(data, dict):
            return {"suggestions": []}
        suggestions = data.get("suggestions")
        if not isinstance(suggestions, list):
            return {"suggestions": []}
        return {"suggestions": suggestions}
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
