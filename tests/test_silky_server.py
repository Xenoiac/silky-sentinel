import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

import silky_server


def stub_snapshot():
    return {
        "summary": {
            "cpu": {"total_cores": 1.0, "used_cores": 0.2, "utilization_percent": 20.0},
            "memory": {"total_gib": 1.0, "used_gib": 0.5, "utilization_percent": 50.0},
            "storage": {"total_gib": 10.0, "used_gib": 1.0, "utilization_percent": 10.0},
            "pods": {
                "total": 1,
                "unhealthy": 0,
                "unhealthy_percent": 0.0,
                "by_status": {"Running": 1},
            },
            "nodes": {"count": 1, "ready": 1, "not_ready": 0},
            "alerts": {"last_severity": "unknown", "open_incidents": 0},
            "queues": {"enabled": False, "total_backlog": 0, "top_queues": []},
            "total_pods": 1,
            "bad_pods": 0,
        },
        "namespaces": {"top_by_cpu": [], "top_by_memory": [], "unhealthy_counts": []},
        "pods": [
            {
                "namespace": "default",
                "name": "pod-a",
                "status": "Running",
                "restarts": 0,
                "age": "1d",
                "node": "node-1",
                "reason": None,
            }
        ],
        "errors": [],
    }


def build_client(monkeypatch):
    monkeypatch.setattr(silky_server, "ensure_kubeconfig", lambda: "/tmp/kubeconfig")
    monkeypatch.setattr(silky_server, "night_collect_cluster_health", lambda: stub_snapshot())
    monkeypatch.setattr(
        silky_server,
        "generate_sre_suggestions",
        lambda snapshot, events, latest: {"suggestions": ["check logs"]},
    )
    monkeypatch.setattr(
        silky_server,
        "summarize_night_mode",
        lambda events, latest: {
            "summary_markdown": "All clear",
            "severity_histogram": {"low": 1},
            "recommendations": [],
        },
    )
    monkeypatch.setattr(
        silky_server,
        "apply_sre_suggestion",
        lambda payload: {
            "status": "ok",
            "summary": f"Ran {payload.get('command', '')}",
            "next_step": "",
            "exit_code": 0,
        },
    )
    monkeypatch.setattr(silky_server, "init_agent_state", lambda *a, **k: {"messages": []})
    monkeypatch.setattr(
        silky_server, "agent_step", lambda state, user_decision=None: {"status": "done", "answer": "complete"}
    )
    monkeypatch.setattr(silky_server, "client", None)
    return TestClient(silky_server.app)



def test_health_endpoint(monkeypatch):
    client = build_client(monkeypatch)
    response = client.get("/api/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_cluster_snapshot(monkeypatch):
    client = build_client(monkeypatch)
    response = client.get("/api/cluster/pods")
    assert response.status_code == 200
    body = response.json()
    assert body["summary"]["total_pods"] == 1
    assert body["pods"][0]["name"] == "pod-a"


def test_apply_suggestion(monkeypatch):
    client = build_client(monkeypatch)
    payload = {"title": "test", "reason": "demo", "action": "cmd", "command": "echo hi"}
    response = client.post("/api/sre/suggestions/apply", json=payload)
    assert response.status_code == 200
    result = response.json()
    assert result["status"] == "ok"
    assert "echo hi" in result["summary"]


def test_agent_start(monkeypatch):
    client = build_client(monkeypatch)
    response = client.post("/api/agent/start", json={"question": "hello?"})
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "done"
    assert "session_id" in data
