from types import SimpleNamespace

from app import chatbot


def test_groq_answer_is_returned(monkeypatch):
    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"choices": [{"message": {"content": "Groq says hello."}}]}

    captured = {}

    def fake_post(url, **kwargs):
        captured["url"] = url
        captured["payload"] = kwargs["json"]
        return Response()

    monkeypatch.setattr(chatbot, "GROQ_API_KEY", "test-key")
    monkeypatch.setattr(chatbot.httpx, "post", fake_post)

    answer = chatbot.answer_question("What is this topic?", SimpleNamespace(
        caption="#goa sunset",
        author=SimpleNamespace(username="alice"),
    ))

    assert answer == "Groq says hello."
    assert captured["payload"]["model"] == chatbot.GROQ_MODEL
    assert "#goa sunset" in captured["payload"]["messages"][1]["content"]


def test_local_fallback_is_used_when_groq_fails(monkeypatch):
    monkeypatch.setattr(chatbot, "GROQ_API_KEY", "test-key")
    monkeypatch.setattr(chatbot.httpx, "post", lambda *args, **kwargs: (_ for _ in ()).throw(chatbot.httpx.TimeoutException("timeout")))

    assert "heart button" in chatbot.answer_question("How do I like a post?")
