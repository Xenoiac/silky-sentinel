import json
from types import SimpleNamespace

import silky_sentinel


class DummyResponses:
    def __init__(self, text: str):
        self._text = text

    def create(self, *args, **kwargs):
        return SimpleNamespace(output_text=self._text)


class DummyClient:
    def __init__(self, text: str):
        self.responses = DummyResponses(text)


def test_summarize_night_mode_ollama_markdown(monkeypatch):
    monkeypatch.setattr(silky_sentinel, "client", DummyClient("## Cluster health looks stable"))
    monkeypatch.setattr(silky_sentinel, "LLM_PROVIDER", "ollama")

    events = [{"analysis": {"severity": "low"}}]

    summary = silky_sentinel.summarize_night_mode(events, latest_report=None)

    assert summary["summary_markdown"].startswith("## Cluster health")
    assert summary["severity_histogram"] == {"low": 1}
    assert summary["provider_mode"] == "ollama_markdown"
    assert summary["llm_parse_error"] is False
    assert summary["severity"] == "unknown"
    assert summary.get("analysis_markdown") == summary["summary_markdown"]


def test_summarize_night_mode_openai_json(monkeypatch):
    payload = {
        "summary_markdown": "All clear",
        "severity_histogram": {"ok": 2},
        "recommendations": ["Nothing to do"],
    }
    monkeypatch.setattr(
        silky_sentinel,
        "client",
        DummyClient(json.dumps(payload)),
    )
    monkeypatch.setattr(silky_sentinel, "LLM_PROVIDER", "openai")

    summary = silky_sentinel.summarize_night_mode([], latest_report=None)

    assert summary["summary_markdown"] == "All clear"
    assert summary["severity_histogram"] == {"ok": 2}
    assert summary["recommendations"] == ["Nothing to do"]
