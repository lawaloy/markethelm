"""OpenAI prompt path must coerce Inf/NaN instead of returning None."""

from unittest.mock import patch

from src.analysis.ai_summarizer import AISummarizer


def test_openai_prompt_coerces_nonfinite_rollups(monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    summarizer = AISummarizer()
    assert summarizer.enabled is True

    analysis = {
        "date": "2026-08-05",
        "summary": {
            "total_stocks": 10,
            "gainers": 4,
            "losers": 6,
            "average_change_percent": float("nan"),
        },
        "top_gainers": [
            {"symbol": "AAA", "name": "Aaa", "change_percent": float("inf")},
            "skip-me",
        ],
        "top_losers": [
            {"symbol": "BBB", "name": "Bbb", "change_percent": None},
        ],
    }
    exchange = {
        "S&P 500": {
            "average_change_percent": float("-inf"),
            "gainers": 1,
            "losers": 2,
        },
        "BAD": "not-a-dict",
    }

    captured: dict = {}

    fake_message = type("Msg", (), {"content": "Markets were mixed."})()
    fake_choice = type("Choice", (), {"message": fake_message})()
    fake_response = type("Resp", (), {"choices": [fake_choice]})()

    class FakeCompletions:
        @staticmethod
        def create(**kwargs):
            captured["kwargs"] = kwargs
            return fake_response

    class FakeClient:
        def __init__(self, api_key=None):
            self.chat = type("Chat", (), {"completions": FakeCompletions()})()

    openai_mod = type("openai", (), {"OpenAI": FakeClient})()
    with patch("src.analysis.ai_summarizer.OpenAI", openai_mod.OpenAI):
        result = summarizer.generate_summary(analysis, exchange)

    assert result == "Markets were mixed."
    prompt = captured["kwargs"]["messages"][1]["content"]
    assert "Average Change: 0.00%" in prompt
    assert "AAA" in prompt
    assert "+0.00%" in prompt
    assert "BBB" in prompt
    assert "S&P 500: Avg 0.00%" in prompt
    assert "skip-me" not in prompt
    assert "BAD" not in prompt
