"""Tests for AI summarizer module."""

import os as os_module
from unittest.mock import patch

from src.analysis.ai_summarizer import AISummarizer


class TestAISummarizerDemoSummary:
    """Test demo summary generation (no API key)."""

    def test_generate_demo_summary_positive_sentiment(self):
        """Demo summary with more gainers than losers shows positive sentiment."""
        summarizer = AISummarizer()
        analysis = {
            "summary": {"gainers": 5, "losers": 2, "average_change_percent": 0.5},
            "top_gainers": [{"symbol": "AAPL", "change_percent": 2.5}],
            "top_losers": [{"symbol": "GOOGL", "change_percent": -1.0}],
        }
        exchange_comparison = {"S&P 500": {"average_change_percent": 0.6}}

        result = summarizer.generate_demo_summary(analysis, exchange_comparison)

        assert "positive" in result
        assert "5 gainers" in result
        assert "2 losers" in result
        assert "AAPL" in result
        assert "GOOGL" in result
        assert "S&P 500" in result

    def test_generate_demo_summary_negative_sentiment(self):
        """Demo summary with more losers than gainers shows negative sentiment."""
        summarizer = AISummarizer()
        analysis = {
            "summary": {"gainers": 2, "losers": 6, "average_change_percent": -0.8},
            "top_gainers": [{"symbol": "MSFT", "change_percent": 0.5}],
            "top_losers": [{"symbol": "META", "change_percent": -3.2}],
        }
        exchange_comparison = {"NASDAQ-100": {"average_change_percent": -0.5}}

        result = summarizer.generate_demo_summary(analysis, exchange_comparison)

        assert "negative" in result
        assert "2 gainers" in result
        assert "6 losers" in result
        assert "MSFT" in result
        assert "META" in result

    def test_generate_demo_summary_mixed_sentiment(self):
        """Demo summary with equal gainers/losers shows mixed sentiment."""
        summarizer = AISummarizer()
        analysis = {
            "summary": {"gainers": 3, "losers": 3, "average_change_percent": 0.0},
            "top_gainers": [],
            "top_losers": [],
        }
        exchange_comparison = {}

        result = summarizer.generate_demo_summary(analysis, exchange_comparison)

        assert "mixed" in result

    def test_generate_demo_summary_empty_exchange_comparison(self):
        """Demo summary works with empty exchange comparison."""
        summarizer = AISummarizer()
        analysis = {
            "summary": {"gainers": 1, "losers": 1, "average_change_percent": 0.0},
            "top_gainers": [{"symbol": "A", "change_percent": 1.0}],
            "top_losers": [{"symbol": "B", "change_percent": -1.0}],
        }
        exchange_comparison = {}

        result = summarizer.generate_demo_summary(analysis, exchange_comparison)

        assert "A" in result
        assert "B" in result

    @patch("src.analysis.ai_summarizer.os.getenv")
    def test_generate_summary_returns_demo_when_no_api_key(self, mock_getenv):
        """generate_summary returns demo summary when OPENAI_API_KEY not set."""

        def fake_getenv(key, default=None):
            if key == "OPENAI_API_KEY":
                return None
            return os_module.environ.get(key, default)

        mock_getenv.side_effect = fake_getenv
        summarizer = AISummarizer()
        analysis = {
            "summary": {"gainers": 2, "losers": 1, "average_change_percent": 0.3},
            "top_gainers": [{"symbol": "X", "change_percent": 1.0}],
            "top_losers": [{"symbol": "Y", "change_percent": -0.5}],
        }
        exchange_comparison = {}

        result = summarizer.generate_summary(analysis, exchange_comparison)

        assert result is not None
        assert "sentiment" in result.lower()


class TestAISummarizerOpenAIPath:
    """OpenAI-enabled path soft-fail and success contracts."""

    def _analysis(self):
        return {
            "date": "2026-07-24",
            "summary": {
                "total_stocks": 2,
                "gainers": 1,
                "losers": 1,
                "average_change_percent": 0.1,
            },
            "top_gainers": [{"symbol": "AAPL", "name": "Apple", "change_percent": 1.0}],
            "top_losers": [{"symbol": "MSFT", "name": "Microsoft", "change_percent": -0.5}],
        }

    @patch.dict("os.environ", {"OPENAI_API_KEY": "sk-test"}, clear=False)
    def test_generate_summary_returns_openai_completion_text(self):
        """When OpenAI succeeds, return stripped completion content (not demo)."""

        summarizer = AISummarizer()
        assert summarizer.enabled is True

        exchange_comparison = {
            "S&P 500": {
                "average_change_percent": 0.2,
                "gainers": 1,
                "losers": 1,
            }
        }

        fake_message = type("Msg", (), {"content": "  Markets were mixed today.  "})()
        fake_choice = type("Choice", (), {"message": fake_message})()
        fake_response = type("Resp", (), {"choices": [fake_choice]})()

        class FakeCompletions:
            @staticmethod
            def create(**_kwargs):
                return fake_response

        class FakeClient:
            def __init__(self, api_key=None):
                self.chat = type("Chat", (), {"completions": FakeCompletions()})()

        openai_mod = type("openai", (), {"OpenAI": FakeClient})()
        with patch("src.analysis.ai_summarizer.OpenAI", openai_mod.OpenAI):
            result = summarizer.generate_summary(self._analysis(), exchange_comparison)

        assert result == "Markets were mixed today."

    @patch.dict("os.environ", {"OPENAI_API_KEY": "sk-test"}, clear=False)
    def test_generate_summary_returns_none_when_openai_import_fails(self):
        """Missing openai package must return None (no silent demo fallback)."""

        summarizer = AISummarizer()
        with patch("src.analysis.ai_summarizer.OpenAI", None):
            result = summarizer.generate_summary(self._analysis(), {})

        assert result is None

    @patch.dict("os.environ", {"OPENAI_API_KEY": "sk-test"}, clear=False)
    def test_generate_summary_returns_none_on_api_exception(self):
        """API failures return None so callers can detect empty AI summary."""

        summarizer = AISummarizer()

        class BoomClient:
            def __init__(self, api_key=None):
                self.chat = self

            @property
            def completions(self):
                return self

            def create(self, **_kwargs):
                raise RuntimeError("rate limited")

        openai_mod = type("openai", (), {"OpenAI": BoomClient})()
        with patch("src.analysis.ai_summarizer.OpenAI", openai_mod.OpenAI):
            result = summarizer.generate_summary(self._analysis(), {})

        assert result is None

    @patch.dict("os.environ", {"OPENAI_API_KEY": "sk-test"}, clear=False)
    def test_generate_summary_returns_none_when_message_content_is_none(self):
        """Null completion content (openai 3.17 parse_response) must fail closed."""

        summarizer = AISummarizer()

        fake_message = type("Msg", (), {"content": None})()
        fake_choice = type("Choice", (), {"message": fake_message})()
        fake_response = type("Resp", (), {"choices": [fake_choice]})()

        class FakeCompletions:
            @staticmethod
            def create(**_kwargs):
                return fake_response

        class FakeClient:
            def __init__(self, api_key=None):
                self.chat = type("Chat", (), {"completions": FakeCompletions()})()

        openai_mod = type("openai", (), {"OpenAI": FakeClient})()
        with patch("src.analysis.ai_summarizer.OpenAI", openai_mod.OpenAI):
            result = summarizer.generate_summary(self._analysis(), {})

        assert result is None

    @patch.dict("os.environ", {"OPENAI_API_KEY": "sk-test"}, clear=False)
    def test_generate_summary_returns_none_when_choices_empty(self):
        """A 200-style response with no choices must not crash the tracker path."""

        summarizer = AISummarizer()
        fake_response = type("Resp", (), {"choices": []})()

        class FakeCompletions:
            @staticmethod
            def create(**_kwargs):
                return fake_response

        class FakeClient:
            def __init__(self, api_key=None):
                self.chat = type("Chat", (), {"completions": FakeCompletions()})()

        openai_mod = type("openai", (), {"OpenAI": FakeClient})()
        with patch("src.analysis.ai_summarizer.OpenAI", openai_mod.OpenAI):
            result = summarizer.generate_summary(self._analysis(), {})

        assert result is None
