import json
from types import SimpleNamespace

import silky_sentinel


class FakeChatCompletions:
    def __init__(self, text: str):
        self._text = text

    def create(self, *args, **kwargs):
        message = SimpleNamespace(content=self._text)
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])


class DummyClient:
    def __init__(self, text: str):
        self.client = SimpleNamespace(chat=SimpleNamespace(completions=FakeChatCompletions(text)))


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


def test_parse_llm_json_strips_code_fences():
    fenced = """```json\n{\"suggestions\": [{\"id\": \"1\"}]}\n```"""
    parsed = silky_sentinel.parse_llm_json(fenced, "ollama", "sre_suggestions")
    assert parsed == {"suggestions": [{"id": "1"}]}


def test_generate_sre_suggestions_fallback_on_parse_error(monkeypatch):
    class DummyCompletions:
        def create(self, *args, **kwargs):
            message = SimpleNamespace(content="Command 1\nCommand 2")
            return SimpleNamespace(choices=[SimpleNamespace(message=message)])

    class DummyClient:
        def __init__(self):
            self.client = SimpleNamespace(chat=SimpleNamespace(completions=DummyCompletions()))

    monkeypatch.setattr(silky_sentinel, "client", DummyClient())
    monkeypatch.setattr(silky_sentinel, "LLM_PROVIDER", "ollama")

    result = silky_sentinel.generate_sre_suggestions({}, [], None)

    assert len(result["suggestions"]) == 2
    assert result["suggestions"][0]["id"].startswith("sug-fallback-")
