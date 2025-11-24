import json
from pathlib import Path
from types import SimpleNamespace

import silky_sentinel


def test_night_collect_cluster_health(monkeypatch):
    pods_output = """default pod-a 1/1 Running 0 10s
default pod-b 0/1 CrashLoopBackOff 6 20s
"""
    nodes_output = """node-1 Ready control-plane 10d v1
node-2 NotReady worker 5d v1
"""
    nodes_json = {
        "items": [
            {"status": {"capacity": {"cpu": "4", "memory": "8Gi"}}},
            {"status": {"capacity": {"cpu": "8", "memory": "16Gi"}}},
        ]
    }
    top_nodes_output = """NAME CPU(cores) CPU% MEMORY(bytes) MEMORY%
node-1 200m 20% 1024Mi 25%
node-2 1000m 50% 2048Mi 50%
"""

    def fake_run_shell_command(cmd):
        if "get pods" in cmd:
            return {"exit_code": 0, "stdout": pods_output, "stderr": ""}
        if "get nodes -o json" in cmd:
            return {"exit_code": 0, "stdout": json.dumps(nodes_json), "stderr": ""}
        if "get nodes" in cmd:
            return {"exit_code": 0, "stdout": nodes_output, "stderr": ""}
        if "top nodes" in cmd:
            return {"exit_code": 0, "stdout": top_nodes_output, "stderr": ""}
        return {"exit_code": 1, "stdout": "", "stderr": "unknown"}

    monkeypatch.setattr(silky_sentinel, "run_shell_command", fake_run_shell_command)

    snapshot = silky_sentinel.night_collect_cluster_health()

    assert snapshot["summary"]["total_pods"] == 2
    assert snapshot["summary"]["bad_pods"] == 1
    assert snapshot["summary"]["nodes"] == {"count": 2, "ready": 1, "not_ready": 1}
    assert snapshot["summary"]["cpu"]["total_cores"] == 12.0
    assert snapshot["summary"]["cpu"]["used_cores"] == 1.2
    assert snapshot["summary"]["cpu"]["utilization_percent"] == 10.0
    assert snapshot["summary"]["memory"]["total_gib"] == 24.0
    assert snapshot["summary"]["memory"]["used_gib"] == 3.0
    assert snapshot["summary"]["memory"]["utilization_percent"] == 12.5
    assert any(
        p["namespace"] == "default" and p["name"] == "pod-a" and p["status"] == "Running" and p["restarts"] == 0
        for p in snapshot["pods"]
    )
    assert any(
        p["namespace"] == "default" and p["name"] == "pod-b" and p["status"] == "CrashLoopBackOff" and p["restarts"] == 6
        for p in snapshot["pods"]
    )


def test_night_analyze_with_llm_parses(monkeypatch):
    class FakeResponse:
        def __init__(self, text):
            self.output_text = text

    class FakeClient:
        def responses_create(self, **kwargs):
            raise NotImplementedError

        def __init__(self, outputs):
            self.outputs = outputs

        def _create(self, **kwargs):
            return FakeResponse(self.outputs.pop(0))

        @property
        def responses(self):
            return SimpleNamespace(create=self._create)

    output = json.dumps(
        {
            "severity": "medium",
            "title": "Test title",
            "summary": "Test summary",
            "notable_pods": [],
            "recommendations": [],
        }
    )

    fake_client = FakeClient([output])
    monkeypatch.setattr(silky_sentinel, "client", fake_client)
    monkeypatch.setattr(silky_sentinel, "OPENAI_API_KEY", "TEST")

    result = silky_sentinel.night_analyze_with_llm({"pods": [], "summary": {}})
    assert result["severity"] == "medium"
    assert result["title"] == "Test title"
    assert result["summary"] == "Test summary"


def test_night_analyze_with_llm_invalid_json(monkeypatch):
    class FakeResponse:
        def __init__(self, text):
            self.output_text = text

    class FakeClient:
        def __init__(self, outputs):
            self.outputs = outputs

        def _create(self, **kwargs):
            return FakeResponse(self.outputs.pop(0))

        @property
        def responses(self):
            return SimpleNamespace(create=self._create)

    fake_client = FakeClient(["{not-json"])
    monkeypatch.setattr(silky_sentinel, "client", fake_client)
    monkeypatch.setattr(silky_sentinel, "OPENAI_API_KEY", "TEST")

    result = silky_sentinel.night_analyze_with_llm({"pods": [], "summary": {}})
    assert result["severity"] == "unknown"
    assert result["title"] == "Failed to parse LLM output"


def test_log_event_and_generate_report(tmp_path, monkeypatch):
    monkeypatch.setattr(silky_sentinel, "NIGHT_LOG_PATH", tmp_path / "night.log")
    monkeypatch.setattr(silky_sentinel, "REPORTS_DIR", tmp_path / "reports")

    snapshot = {"pods": [], "summary": {"total_pods": 0, "bad_pods": 0}}
    analysis = {"severity": "low", "title": "ok", "summary": "all good", "notable_pods": []}

    silky_sentinel.log_night_event(snapshot, analysis)
    silky_sentinel.log_night_event(snapshot, analysis)

    report_text, report_file = silky_sentinel.generate_night_mode_report()

    assert "Night Mode Final Report" in report_text
    assert "Severity distribution over the session" in report_text
    assert "Last snapshot cluster summary:" in report_text

    report_path = Path(report_file)
    assert report_path.exists()
    assert report_path.name.startswith("night_mode_report_")
