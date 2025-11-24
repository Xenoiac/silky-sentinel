"""
Minimal HTTP API and static file server for Silky Sentinel.

This app mirrors the configuration of silky_sentinel.py and exposes
simple REST endpoints plus a lightweight chat handler.
"""
import json
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, List

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from silky_sentinel import (
    NIGHT_LOG_PATH,
    REPORTS_DIR,
    NIGHT_INTERVAL_SECONDS,
    client,
    ensure_kubeconfig,
    generate_sre_suggestions,
    night_collect_cluster_health,
    night_mode_loop,
    summarize_night_mode,
    truncate_for_model,
    LLM_MODEL,
    apply_sre_suggestion,
    build_sre_system_prompt,
    build_unified_sre_context,
    AgentState,
    get_llm_client,
    extract_text_from_responses,
    init_web_agent_state,
    web_agent_step,
)
from uuid import uuid4

# ---------------------------------------------------------------------------
# FastAPI app setup
# ---------------------------------------------------------------------------
app = FastAPI(title="Silky Sentinel Server", version="0.1.0")

# Allow local dev origins
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Static files
BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
STATIC_DIR.mkdir(parents=True, exist_ok=True)
REPORTS_DIR.mkdir(parents=True, exist_ok=True)

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

# Night mode tracking
night_thread = None
night_stop_event = None
night_start_time = None
agent_sessions: Dict[str, AgentState] = {}
suggestion_chat_sessions: Dict[str, List[Dict[str, Any]]] = {}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _night_running():
    return night_thread is not None and night_thread.is_alive()


def read_night_events(limit: int = 50):
    if not NIGHT_LOG_PATH.exists():
        return []

    entries = []
    with open(NIGHT_LOG_PATH, "r") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue

    return list(reversed(entries[-limit:]))


def safe_report_path(filename: str) -> Path:
    requested = Path(filename)
    if requested.is_absolute() or ".." in requested.parts:
        raise HTTPException(status_code=400, detail="Invalid filename")
    resolved = (REPORTS_DIR / requested).resolve()
    if REPORTS_DIR not in resolved.parents and resolved != REPORTS_DIR:
        raise HTTPException(status_code=400, detail="Invalid filename")
    return resolved


def latest_report_text_or_empty() -> str:
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


def _format_agent_step_response(session_id: str, result: Dict[str, Any]) -> Dict[str, Any]:
    """Return a lightweight response for the web console."""

    response: Dict[str, Any] = {
        "session_id": session_id,
        "status": result.get("status"),
    }

    action = result.get("action")
    if action:
        response["action"] = action

    if result.get("status") == "need_approval":
        response.update(
            {
                "command": result.get("command"),
                "proposed_command": result.get("command"),
                "reason": result.get("reason"),
                "log_path": result.get("log_path"),
                "keywords": result.get("keywords"),
                "context_lines": result.get("context_lines"),
                "max_snippets": result.get("max_snippets"),
            }
        )

    if result.get("status") == "intermediate":
        response["note"] = result.get("note")

    if result.get("status") == "done":
        response["final_answer"] = result.get("final_answer")

    ran = result.get("ran")
    if ran:
        response["ran"] = {
            "type": ran.get("type"),
            "command": ran.get("command"),
        }
        if ran.get("type") == "command":
            response["command_output"] = ran.get("summary")
            response["highlights"] = ran.get("highlights") or []

    return response


def _normalize_user_decision(decision: Dict[str, Any]) -> Dict[str, Any]:
    """Massage client decisions into the format expected by the agent engine."""

    if not decision:
        return {}

    normalized = dict(decision)
    decision_type = normalized.get("type")
    action = normalized.get("action")
    command = normalized.get("command")
    log_path = normalized.get("log_path")

    if decision_type in {"approve", "deny"}:
        if not action and command:
            action = "run_command"
        proposal: Dict[str, Any] = {
            "action": action,
            "command": command,
            "log_path": log_path,
            "keywords": normalized.get("keywords"),
            "context_lines": normalized.get("context_lines"),
            "max_snippets": normalized.get("max_snippets"),
        }
        normalized["proposal"] = proposal

    return normalized


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@app.get("/", response_class=FileResponse)
def root():
    index_path = STATIC_DIR / "index.html"
    if not index_path.exists():
        raise HTTPException(status_code=404, detail="index.html not found")
    return FileResponse(index_path)


@app.get("/api/health")
def health():
    return {"status": "ok", "timestamp": time.strftime("%Y-%m-%d %H:%M:%S")}


@app.get("/api/cluster/pods")
def cluster_pods():
    try:
        ensure_kubeconfig()
    except Exception as exc:  # pragma: no cover - runtime validation
        raise HTTPException(status_code=500, detail=str(exc))

    snapshot = night_collect_cluster_health()
    return snapshot


@app.get("/api/cluster/metrics")
def cluster_metrics():
    try:
        ensure_kubeconfig()
    except Exception as exc:  # pragma: no cover - runtime validation
        raise HTTPException(status_code=500, detail=str(exc))

    snapshot = night_collect_cluster_health()
    return snapshot.get("summary", {})


@app.get("/api/night/events")
def night_events(limit: int = 50):
    events = read_night_events(limit)
    return events


@app.get("/api/night/summary")
def night_summary():
    events = read_night_events(limit=120)
    latest = latest_report_text_or_empty()
    return summarize_night_mode(events, latest)


@app.get("/api/sre/suggestions")
def sre_suggestions():
    try:
        ensure_kubeconfig()
    except Exception as exc:  # pragma: no cover - runtime validation
        raise HTTPException(status_code=500, detail=str(exc))

    snapshot = night_collect_cluster_health()
    events = read_night_events(limit=120)
    latest = latest_report_text_or_empty()
    return generate_sre_suggestions(snapshot, events, latest)


@app.post("/api/sre/suggestions/apply")
def api_apply_suggestion(payload: Dict[str, Any]):
    """
    Execute an SRE suggestion and summarize its outcome.

    Body JSON:
      { "title": str, "reason": str, "action": str, "command": str }
    """
    try:
        suggestion = {
            "title": payload.get("title", ""),
            "reason": payload.get("reason", ""),
            "action": payload.get("action", ""),
            "command": payload.get("command", ""),
        }
        result = apply_sre_suggestion(suggestion)
        return result
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to apply suggestion: {exc}")


@app.post("/api/sre/suggestions/chat/start")
def api_suggestion_chat_start(payload: Dict[str, Any]):
    """
    Start a mini chat session for a specific SRE suggestion.

    Body:
      { "title": str, "reason": str, "action": str, "command": str }
    """
    context = {
        "suggestion_title": payload.get("title", ""),
        "suggestion_reason": payload.get("reason", ""),
        "suggestion_action": payload.get("action", ""),
        "command": payload.get("command", ""),
        "unified_context": build_unified_sre_context(),
    }
    system_prompt = build_sre_system_prompt(context)
    user_prompt = (
        "We are starting a discussion about this SRE suggestion for a Kubernetes cluster. "
        "In 1-2 short sentences, summarize what this suggestion is about and invite questions."
    )

    client = get_llm_client()
    completion = client.responses.create(
        model=LLM_MODEL,
        input=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
    )
    assistant_text = extract_text_from_responses(completion).strip()

    session_id = str(uuid4())
    suggestion_chat_sessions[session_id] = [
        {"role": "system", "content": system_prompt},
        {"role": "assistant", "content": assistant_text},
    ]

    return {"session_id": session_id, "assistant": assistant_text}


@app.post("/api/sre/suggestions/chat/step")
def api_suggestion_chat_step(payload: Dict[str, Any]):
    """
    Continue a mini chat session.

    Body:
      { "session_id": str, "message": str }
    """
    session_id = payload.get("session_id")
    message = (payload.get("message") or "").strip()
    if not session_id or session_id not in suggestion_chat_sessions:
        raise HTTPException(status_code=404, detail="Suggestion chat session not found")
    if not message:
        raise HTTPException(status_code=400, detail="Empty message")

    msgs = suggestion_chat_sessions[session_id]
    msgs.append({"role": "user", "content": message})

    client = get_llm_client()
    completion = client.responses.create(
        model=LLM_MODEL,
        input=msgs,
    )
    assistant_text = extract_text_from_responses(completion).strip()
    msgs.append({"role": "assistant", "content": assistant_text})
    suggestion_chat_sessions[session_id] = msgs

    return {"assistant": assistant_text}


@app.post("/api/night/start")
def night_start():
    global night_thread, night_stop_event, night_start_time

    if _night_running():
        raise HTTPException(status_code=400, detail="Night Mode is already running")

    night_stop_event = threading.Event()

    def runner():
        night_mode_loop(interval_seconds=NIGHT_INTERVAL_SECONDS, stop_event=night_stop_event)

    night_thread = threading.Thread(target=runner, name="night-mode-thread", daemon=True)
    night_thread.start()
    night_start_time = datetime.utcnow()

    return {"status": "started", "interval_seconds": NIGHT_INTERVAL_SECONDS}


@app.post("/api/night/stop")
def night_stop():
    global night_thread, night_stop_event, night_start_time

    if not _night_running():
        raise HTTPException(status_code=400, detail="Night Mode is not running")

    if night_stop_event:
        night_stop_event.set()

    night_thread.join(timeout=5)

    stopped_cleanly = not night_thread.is_alive()

    if stopped_cleanly:
        night_thread = None
        night_stop_event = None
        night_start_time = None

    return {"status": "stopped", "stopped_cleanly": stopped_cleanly}


@app.get("/api/night/status")
def night_status():
    running = _night_running()
    start_time_str = night_start_time.isoformat() if night_start_time else None
    return {
        "running": running,
        "start_time": start_time_str,
        "interval_seconds": NIGHT_INTERVAL_SECONDS,
    }


@app.get("/api/night/reports")
def night_reports():
    reports = []
    for file in REPORTS_DIR.glob("**/*"):
        if file.is_file():
            stats = file.stat()
            reports.append(
                {
                    "filename": str(file.relative_to(REPORTS_DIR)),
                    "timestamp": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(stats.st_mtime)),
                    "size": stats.st_size,
                }
            )

    reports.sort(key=lambda r: r["timestamp"], reverse=True)
    return reports


@app.get("/api/night/report/{filename}")
def get_report(filename: str):
    path = safe_report_path(filename)
    if not path.exists() or not path.is_file():
        raise HTTPException(status_code=404, detail="Report not found")

    content = path.read_text(errors="replace")

    try:
        parsed = json.loads(content)
        return JSONResponse(parsed)
    except json.JSONDecodeError:
        return PlainTextResponse(content)


@app.post("/api/chat")
def chat(payload: dict):
    message = (payload or {}).get("message", "").strip()
    if not message:
        raise HTTPException(status_code=400, detail="message is required")

    if client is None:
        # Fallback if model is not configured
        reply = "OpenAI client is not configured. Please set OPENAI_API_KEY."
        return {"answer": reply}

    system_prompt = (
        "You are Silky Sentinel, a concise SRE assistant. "
        "Provide a short, helpful answer without proposing shell commands."
    )

    response = client.responses.create(
        model=LLM_MODEL,
        input=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": truncate_for_model(message)},
        ],
    )

    raw_answer = response.output_text.strip()
    cleaned = raw_answer.replace("```json", "").replace("```", "").strip()
    return {"answer": cleaned}


@app.post("/api/agent/start")
def agent_start(payload: dict):
    question = (payload or {}).get("question", "")
    if not question:
        raise HTTPException(status_code=400, detail="question is required")

    state = init_web_agent_state(question)
    session_id = str(uuid4())
    result = web_agent_step(state, user_decision=None)
    agent_sessions[session_id] = state
    return _format_agent_step_response(session_id, result)


@app.post("/api/agent/step")
def agent_step_api(payload: dict):
    session_id = (payload or {}).get("session_id")
    decision = (payload or {}).get("decision")
    if not session_id:
        raise HTTPException(status_code=400, detail="session_id is required")
    if decision is None:
        raise HTTPException(status_code=400, detail="decision is required")

    state = agent_sessions.get(session_id)
    if state is None:
        raise HTTPException(status_code=404, detail="session not found")

    normalized_decision = _normalize_user_decision(decision)
    result = web_agent_step(state, user_decision=normalized_decision)
    agent_sessions[session_id] = state
    return _format_agent_step_response(session_id, result)


# Run with: uvicorn silky_server:app --reload
