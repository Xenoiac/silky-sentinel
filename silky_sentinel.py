import os
import sys
import json
import time
import subprocess
import requests
import re
from pathlib import Path

from dotenv import load_dotenv
from openai import OpenAI

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


def run_shell_command(command: str) -> dict:
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


def night_collect_cluster_health() -> dict:
    snapshot = {"pods": [], "summary": {}, "errors": []}

    cmd = "kubectl get pods -A --no-headers"
    result = run_shell_command(cmd)

    if result["exit_code"] != 0:
        snapshot["errors"].append(
            {"collector": "pods", "error": result["stderr"] or "unknown error"}
        )
        return snapshot

    lines = result["stdout"].splitlines()
    total = 0
    bad = 0

    for line in lines:
        parts = line.split()
        if len(parts) < 5:
            continue

        ns, name, ready, status, restarts = parts[0], parts[1], parts[2], parts[3], parts[4]

        try:
            restarts = int(restarts)
        except:
            restarts = -1

        pod = {
            "namespace": ns,
            "name": name,
            "ready": ready,
            "status": status,
            "restarts": restarts,
        }
        snapshot["pods"].append(pod)

        total += 1
        if status not in ("Running", "Completed") or restarts > 5:
            bad += 1

    snapshot["summary"] = {"total_pods": total, "bad_pods": bad}
    return snapshot


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

    lines.append("Last snapshot cluster summary:")
    lines.append(f"  - Total pods : {last_summary.get('total_pods', 'N/A')}")
    lines.append(f"  - Bad pods   : {last_summary.get('bad_pods', 'N/A')}")
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


def night_mode_loop(interval_seconds: int = 300):
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

            time.sleep(interval_seconds)

    except KeyboardInterrupt:
        # User hit Ctrl+C: generate final session report
        print("\n🌙 Night Mode interrupted by user (Ctrl+C). Generating final report...\n")
        report_text, report_file = generate_night_mode_report()
        print(report_text)
        print(f"\n📄 Night Mode report saved to: {report_file}")
        notify_admin(f"Night Mode stopped by user; report saved to {report_file}", "INFO")


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
