import os
import subprocess
import sys
import tempfile
import types
from types import SimpleNamespace

# Ensure the requests dependency is stubbed before importing silky_sentinel
if "requests" not in sys.modules:
    sys.modules["requests"] = types.SimpleNamespace(post=lambda *args, **kwargs: None)
if "dotenv" not in sys.modules:
    sys.modules["dotenv"] = types.SimpleNamespace(load_dotenv=lambda *args, **kwargs: None)
if "openai" not in sys.modules:
    class _DummyOpenAI:
        def __init__(self, *args, **kwargs):
            pass
    sys.modules["openai"] = types.SimpleNamespace(OpenAI=_DummyOpenAI)

# Avoid exit during import when OPENAI_API_KEY is missing
os.environ.setdefault("OPENAI_API_KEY", "DUMMY_KEY_FOR_MOCK_DEMO")

import pytest


@pytest.fixture
def tmp_env(monkeypatch):
    original_env = os.environ.copy()
    fake_kube = tempfile.mktemp(prefix="kubeconfig-")

    monkeypatch.setenv("OPENAI_API_KEY", "DUMMY_KEY_FOR_MOCK_DEMO")
    monkeypatch.setenv("KUBECONFIG", fake_kube)

    yield

    # restore original environment
    for key in list(os.environ.keys()):
        if key not in original_env:
            del os.environ[key]
    os.environ.update(original_env)


@pytest.fixture
def mock_subprocess_run(monkeypatch):
    state = {
        "returncode": 0,
        "stdout": "",
        "stderr": "",
        "side_effect": None,
    }

    def fake_run(*args, **kwargs):
        if state["side_effect"]:
            raise state["side_effect"]
        return SimpleNamespace(
            returncode=state["returncode"],
            stdout=state["stdout"],
            stderr=state["stderr"],
        )

    def set_result(returncode=0, stdout="", stderr=""):
        state.update({"returncode": returncode, "stdout": stdout, "stderr": stderr})

    def set_exception(exc):
        state.update({"side_effect": exc})

    monkeypatch.setattr(subprocess, "run", fake_run)

    return SimpleNamespace(set_result=set_result, set_exception=set_exception)


@pytest.fixture(autouse=True)
def stub_requests_module(monkeypatch):
    """Provide a lightweight stub for the requests module used at import time."""
    fake_requests = types.SimpleNamespace(post=lambda *args, **kwargs: None)
    monkeypatch.setitem(sys.modules, "requests", fake_requests)
