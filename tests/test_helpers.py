import json
import os
from pathlib import Path

import pytest

import silky_sentinel


def test_truncate_for_model_no_truncate():
    text = "short text"
    assert silky_sentinel.truncate_for_model(text, max_chars=20) == text


def test_truncate_for_model_truncates():
    text = "x" * 10
    truncated = silky_sentinel.truncate_for_model(text, max_chars=5)
    assert truncated.startswith("x" * 5)
    assert truncated.endswith("...[truncated, original length 10 chars]...")
    assert len(truncated.split("\n\n")[0]) == 5


def test_ensure_kubeconfig_success(tmp_path, monkeypatch):
    fake_kube = tmp_path / "config"
    fake_kube.write_text("apiVersion: v1\n")
    monkeypatch.setenv("KUBECONFIG", str(fake_kube))

    path = silky_sentinel.ensure_kubeconfig()

    assert path == str(fake_kube.resolve())
    assert os.environ["KUBECONFIG"] == str(fake_kube.resolve())


def test_ensure_kubeconfig_missing(monkeypatch):
    monkeypatch.delenv("KUBECONFIG", raising=False)
    with pytest.raises(RuntimeError):
        silky_sentinel.ensure_kubeconfig()


def test_ensure_kubeconfig_nonexistent(tmp_path, monkeypatch):
    missing = tmp_path / "does-not-exist"
    monkeypatch.setenv("KUBECONFIG", str(missing))
    with pytest.raises(RuntimeError):
        silky_sentinel.ensure_kubeconfig()


def test_analyze_logs_locally_missing(tmp_path):
    missing_log = tmp_path / "absent.log"
    digest = silky_sentinel.analyze_logs_locally(str(missing_log))
    assert "Log file not found" in digest


def test_analyze_logs_locally_with_matches(tmp_path):
    log_file = tmp_path / "app.log"
    log_file.write_text(
        """
info starting service
ERROR something broke
info continuing
Exception another issue
"""
    )

    digest = silky_sentinel.analyze_logs_locally(str(log_file), keywords=["ERROR", "Exception"], max_snippets=1)

    assert "Total lines scanned" in digest
    assert "Lines matching patterns" in digest
    assert "SNIPPET #1" in digest
    assert "ERROR something broke" in digest
    # only one snippet due to max_snippets
    assert digest.count("SNIPPET #") == 1
