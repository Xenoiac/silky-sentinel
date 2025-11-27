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


def test_generate_sre_suggestions_salvages_ollama_json(monkeypatch):
    monkeypatch.setenv("OPENAI_BASE_URL", "https://ollama.local")
    monkeypatch.setattr(silky_sentinel, "LLM_MODEL", "qwen3:32b")
    monkeypatch.setattr(
        silky_sentinel,
        "client",
        DummyClient("Result: {\"suggestions\": [{\"id\": \"1\"}]} Extra text"),
    )

    result = silky_sentinel.generate_sre_suggestions({}, [], None)

    assert result == {"suggestions": [{"id": "1"}]}
