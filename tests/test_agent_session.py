import json
from types import SimpleNamespace

import pytest

import silky_sentinel


class FakeResponse:
    def __init__(self, text):
        self.output_text = text


class QueueClient:
    def __init__(self, outputs):
        self.outputs = list(outputs)

    def _create(self, **kwargs):
        if not self.outputs:
            raise AssertionError("No more responses queued")
        return FakeResponse(self.outputs.pop(0))

    @property
    def responses(self):
        return SimpleNamespace(create=self._create)


def test_agent_session_final_answer(monkeypatch, capsys):
    monkeypatch.setattr(silky_sentinel, "OPENAI_API_KEY", "TEST")
    monkeypatch.setattr(silky_sentinel, "client", QueueClient([json.dumps({"action": "final_answer", "content": "done"})]))

    silky_sentinel.agent_session("hi", max_steps=1)

    captured = capsys.readouterr()
    assert "FINAL ANSWER" in captured.out
    assert "done" in captured.out


def test_agent_session_denied_command(monkeypatch, capsys):
    responses = [
        json.dumps({
            "action": "run_command",
            "command": "echo hello",
            "reason": "test",
        }),
        json.dumps({"action": "final_answer", "content": "finished"}),
    ]
    monkeypatch.setattr(silky_sentinel, "OPENAI_API_KEY", "TEST")
    fake_client = QueueClient(responses)
    monkeypatch.setattr(silky_sentinel, "client", fake_client)

    calls = []

    def fake_run_shell_command(cmd):
        calls.append(cmd)
        return {"exit_code": 0, "stdout": "hi", "stderr": ""}

    monkeypatch.setattr(silky_sentinel, "run_shell_command", fake_run_shell_command)

    user_inputs = iter(["n"])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(user_inputs))

    silky_sentinel.agent_session("hi", max_steps=2)

    captured = capsys.readouterr()
    assert "final answer" in captured.out.lower()
    assert calls == []


def test_agent_session_invalid_json(monkeypatch, capsys):
    monkeypatch.setattr(silky_sentinel, "OPENAI_API_KEY", "TEST")
    monkeypatch.setattr(silky_sentinel, "client", QueueClient(["{bad-json"]))

    notices = []

    def fake_notify(msg, level="INFO"):
        notices.append((level, msg))

    monkeypatch.setattr(silky_sentinel, "notify_admin", fake_notify)

    silky_sentinel.agent_session("hi", max_steps=1)

    assert any(level == "ERROR" for level, _ in notices)
    captured = capsys.readouterr()
    assert "RAW MODEL OUTPUT" in captured.out
