from backend import embeddings
from backend import llm


def test_llm_failure_log_never_echoes_provider_error_text(capsys):
    class BrokenCompletions:
        def create(self, **_kwargs):
            raise RuntimeError("provider response contains api_key=secret-value")

    client = type("Client", (), {"chat": type("Chat", (), {"completions": BrokenCompletions()})()})()

    assert llm._chat_json(client, "system", "user") is None

    output = capsys.readouterr().out
    assert "secret-value" not in output
    assert "RuntimeError" in output


def test_embedding_failure_log_never_echoes_provider_error_text(monkeypatch, capsys):
    monkeypatch.setenv("EMBEDDING_API_KEY", "configured")
    monkeypatch.setenv("EMBEDDING_MODEL", "embedding-model")

    class BrokenEmbeddings:
        def create(self, **_kwargs):
            raise RuntimeError("provider response contains token=secret-value")

    class BrokenClient:
        def __init__(self, **_kwargs):
            self.embeddings = BrokenEmbeddings()

    import openai
    monkeypatch.setattr(openai, "OpenAI", BrokenClient)

    assert embeddings.embed_texts(["candidate query"]) is None

    output = capsys.readouterr().out
    assert "secret-value" not in output
    assert "RuntimeError" in output
