# Silky Sentinel

Silky Sentinel is a lightweight SRE copilot for Kubernetes clusters. It can chat in a terminal, execute shell commands with explicit user approval, and run a "Night Mode" loop that captures cluster health snapshots and summarizes them with an LLM. A minimal FastAPI server exposes the same primitives for UI integrations.

## Features
- **Chat & command approval**: interactive CLI that proposes commands and asks for confirmation before running anything.
- **Night Mode monitoring**: periodic pod/node/usage snapshots plus optional LLM summaries and saved reports.
- **Local log analysis**: extract error-focused snippets from large log files without uploading them.
- **HTTP API**: FastAPI app serving cluster snapshots, Night Mode history, and chat/agent endpoints.
- **Slack-compatible notifications**: optional webhook for operational alerts.

## Getting started
1. **Install dependencies**
   ```bash
   pip install -r requirements.txt  # or pip install fastapi uvicorn python-dotenv openai
   ```

2. **Configure environment**
   Create a `.env` file next to `silky_sentinel.py`:
   ```env
   OPENAI_API_KEY=your-key
   LLM_MODEL=gpt-5.1
   KUBECONFIG=/path/to/kubeconfig
   SILKY_MODE=chat  # or "night"
   NIGHT_INTERVAL_SECONDS=300
   SLACK_WEBHOOK_URL=https://hooks.slack.com/services/...
   ```
   The CLI exits early if `KUBECONFIG` is missing or points to a nonexistent file. When `OPENAI_API_KEY` is omitted, the app runs in mock-only mode.

3. **Run the CLI**
   ```bash
   python silky_sentinel.py
   ```
   - `SILKY_MODE=chat` starts the interactive shell.
   - `SILKY_MODE=night` starts autonomous monitoring; press `Ctrl+C` to generate a final report.

4. **Run the FastAPI server**
   ```bash
   uvicorn silky_server:app --reload --port 8000
   ```
   The default UI lives in `static/index.html`, and REST endpoints are namespaced under `/api` (for example, `/api/cluster/pods` and `/api/night/status`).

## Configuring the LLM provider
- **OpenAI (default)**: set `LLM_PROVIDER=openai` and provide `OPENAI_API_KEY`. The app will use `LLM_MODEL` for all calls.
- **Ollama**: set `LLM_PROVIDER=ollama`, point `LLM_API_BASE` to your Ollama endpoint (default `https://ollama.silky.systems`), and set `LLM_MODEL` to the local model name (e.g., `llama3.1`).
If `LLM_PROVIDER` is unrecognized, the app logs an error and falls back to the OpenAI settings when available.

## Testing
Run the unit suite with:
```bash
pytest -q
```
Fixtures in `tests/conftest.py` stub networked dependencies so the suite runs entirely offline.

## Project layout
- `silky_sentinel.py` — CLI entry point and core logic (LLM prompts, Night Mode, log analyzer, command execution helpers).
- `silky_server.py` — FastAPI server wrapping the core helpers.
- `static/` — lightweight HTML/JS/CSS assets for the web UI.
- `tests/` — Pytest suite covering helpers, audit logging, Night Mode logic, the agent loop, and the HTTP API.

## Operational notes
- Audit logs write to `audit.log`; Night Mode events accumulate in `night_mode_events.log`; generated reports are stored under `reports/`.
- To keep LLM prompts concise, large inputs are truncated automatically.
- Slack notifications are best-effort: failures are logged to stdout rather than raising.

