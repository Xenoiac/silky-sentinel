import json
from pathlib import Path

import silky_sentinel


def test_run_shell_command_audit_success(tmp_path, monkeypatch, mock_subprocess_run):
    audit_log = tmp_path / "audit.log"
    monkeypatch.setattr(silky_sentinel, "AUDIT_LOG_PATH", audit_log)

    mock_subprocess_run.set_result(returncode=0, stdout="hello", stderr="")

    result = silky_sentinel.run_shell_command("echo test-audit")

    assert result["exit_code"] == 0
    assert result["stdout"] == "hello"
    assert result["stderr"] == ""
    assert audit_log.exists()

    contents = audit_log.read_text().strip().splitlines()
    assert len(contents) == 1
    entry = json.loads(contents[0])
    assert entry["command"] == "echo test-audit"
    assert entry["exit_code"] == 0
    assert entry["stdout_snippet"] == "hello"


def test_run_shell_command_audit_exception(tmp_path, monkeypatch, mock_subprocess_run):
    audit_log = tmp_path / "audit.log"
    monkeypatch.setattr(silky_sentinel, "AUDIT_LOG_PATH", audit_log)

    mock_subprocess_run.set_exception(RuntimeError("boom"))

    result = silky_sentinel.run_shell_command("echo test-audit")

    assert result["exit_code"] == -1
    assert "Exception while running command" in result["stderr"]
    assert audit_log.exists()
    entry = json.loads(audit_log.read_text().strip().splitlines()[0])
    assert entry["exit_code"] == -1
    assert "Exception while running command" in entry["stderr_snippet"]
