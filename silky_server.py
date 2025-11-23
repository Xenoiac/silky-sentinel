"""
Minimal HTTP API and static file server for Silky Sentinel.

This app mirrors the configuration of silky_sentinel.py and exposes
simple REST endpoints plus a lightweight chat handler.
"""
import json
import time
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from silky_sentinel import (
    NIGHT_LOG_PATH,
    REPORTS_DIR,
    client,
    ensure_kubeconfig,
    night_collect_cluster_health,
    truncate_for_model,
    LLM_MODEL,
)

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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
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


@app.get("/api/night/events")
def night_events(limit: int = 50):
    events = read_night_events(limit)
    return events


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


# Run with: uvicorn silky_server:app --reload
