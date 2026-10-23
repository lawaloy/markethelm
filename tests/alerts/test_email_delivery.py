"""Tests for transactional email providers (SendGrid, Mailgun)."""

from unittest.mock import MagicMock, patch

import requests

from src.alerts.notifiers.email_delivery import (
    MailgunEmailBackend,
    build_mailgun_backend,
    build_sendgrid_backend,
    build_smtp_backend,
    email_delivery_configured,
    resolve_email_provider,
)
from src.alerts.notifiers.email_notifier import EmailNotifier


@patch("src.alerts.notifiers.email_delivery.requests.post")
@patch.dict(
    "os.environ",
    {
        "ALERT_EMAIL_PROVIDER": "sendgrid",
        "SENDGRID_API_KEY": "sg-test-key",
        "ALERT_EMAIL_FROM": "alerts@markethelm.example",
        "ALERT_EMAIL_TO": "user@example.com",
    },
    clear=True,
)
def test_sendgrid_delivery_success(mock_post: MagicMock) -> None:
    mock_post.return_value = MagicMock(status_code=202, text="")

    notifier = EmailNotifier.from_alert({"id": "a1", "notifications": ["email"]})
    assert notifier is not None
    assert email_delivery_configured() is True

    event = {
        "alert_id": "a1",
        "alert_name": "Price watch",
        "symbols": ["AAPL"],
        "condition_type": "price_threshold",
        "timestamp": "2026-06-17T12:00:00",
    }
    assert notifier.send(event) is True

    mock_post.assert_called_once()
    call_kwargs = mock_post.call_args.kwargs
    assert call_kwargs["headers"]["Authorization"] == "Bearer sg-test-key"
    assert call_kwargs["json"]["from"] == {"email": "alerts@markethelm.example"}
    assert call_kwargs["json"]["personalizations"][0]["to"] == [{"email": "user@example.com"}]


@patch("src.alerts.notifiers.email_delivery.requests.post")
@patch.dict(
    "os.environ",
    {
        "ALERT_EMAIL_PROVIDER": "sendgrid",
        "SENDGRID_API_KEY": "sg-test-key",
        "ALERT_EMAIL_FROM": "alerts@markethelm.example",
        "ALERT_EMAIL_TO": "user@example.com",
    },
    clear=True,
)
def test_sendgrid_delivery_failure(mock_post: MagicMock) -> None:
    mock_post.return_value = MagicMock(status_code=403, text="Forbidden")

    notifier = EmailNotifier.from_alert({"id": "a1", "notifications": ["email"]})
    assert notifier is not None
    assert notifier.send({"alert_id": "a1", "alert_name": "Test", "symbols": []}) is False
    mock_post.assert_called_once()


@patch("src.alerts.notifiers.email_delivery.requests.post")
@patch.dict(
    "os.environ",
    {
        "ALERT_EMAIL_PROVIDER": "sendgrid",
        "SENDGRID_API_KEY": "sg-test-key",
        "ALERT_EMAIL_FROM": "alerts@markethelm.example",
        "ALERT_EMAIL_TO": "user@example.com",
    },
    clear=True,
)
def test_sendgrid_retries_transient_failure(mock_post: MagicMock) -> None:
    mock_post.side_effect = [
        MagicMock(status_code=503, text="unavailable"),
        MagicMock(status_code=202, text=""),
    ]
    notifier = EmailNotifier.from_alert({"id": "a1", "notifications": ["email"]})
    assert notifier is not None
    with patch("src.alerts.notifiers.delivery_retry.time.sleep"):
        assert notifier.send({"alert_id": "a1", "alert_name": "Test", "symbols": []}) is True
    assert mock_post.call_count == 2


@patch("src.alerts.notifiers.email_delivery.requests.post")
@patch.dict(
    "os.environ",
    {
        "ALERT_EMAIL_PROVIDER": "mailgun",
        "MAILGUN_API_KEY": "mg-test-key",
        "MAILGUN_DOMAIN": "mg.markethelm.example",
        "ALERT_EMAIL_FROM": "alerts@markethelm.example",
        "ALERT_EMAIL_TO": "user@example.com",
    },
    clear=True,
)
def test_mailgun_delivery_success(mock_post: MagicMock) -> None:
    mock_post.return_value = MagicMock(status_code=200, text='{"id":"<2026>"}')

    notifier = EmailNotifier.from_alert({"id": "a1", "notifications": ["email"]})
    assert notifier is not None

    assert notifier.send({"alert_id": "a1", "alert_name": "Test", "symbols": ["MSFT"]}) is True

    mock_post.assert_called_once()
    assert mock_post.call_args.args[0].endswith("/v3/mg.markethelm.example/messages")
    assert mock_post.call_args.kwargs["auth"] == ("api", "mg-test-key")
    assert mock_post.call_args.kwargs["data"]["from"] == "alerts@markethelm.example"


@patch("src.alerts.notifiers.email_delivery.requests.post")
@patch.dict(
    "os.environ",
    {
        "ALERT_EMAIL_PROVIDER": "mailgun",
        "MAILGUN_API_KEY": "mg-test-key",
        "MAILGUN_DOMAIN": "mg.markethelm.example",
        "ALERT_EMAIL_FROM": "alerts@markethelm.example",
        "ALERT_EMAIL_TO": "user@example.com",
    },
    clear=True,
)
def test_mailgun_delivery_failure(mock_post: MagicMock) -> None:
    mock_post.return_value = MagicMock(status_code=401, text="Unauthorized")

    notifier = EmailNotifier.from_alert({"id": "a1", "notifications": ["email"]})
    assert notifier is not None
    assert notifier.send({"alert_id": "a1", "alert_name": "Test", "symbols": []}) is False
    mock_post.assert_called_once()


@patch("src.alerts.notifiers.email_delivery.requests.post")
@patch.dict(
    "os.environ",
    {
        "ALERT_EMAIL_PROVIDER": "mailgun",
        "MAILGUN_API_KEY": "mg-test-key",
        "MAILGUN_DOMAIN": "mg.markethelm.example",
        "ALERT_EMAIL_FROM": "alerts@markethelm.example",
        "ALERT_EMAIL_TO": "user@example.com",
    },
    clear=True,
)
def test_mailgun_retries_transient_failure(mock_post: MagicMock) -> None:
    mock_post.side_effect = [
        MagicMock(status_code=503, text="unavailable"),
        MagicMock(status_code=200, text='{"id":"<ok>"}'),
    ]
    notifier = EmailNotifier.from_alert({"id": "a1", "notifications": ["email"]})
    assert notifier is not None
    with patch("src.alerts.notifiers.delivery_retry.time.sleep"):
        assert notifier.send({"alert_id": "a1", "alert_name": "Test", "symbols": []}) is True
    assert mock_post.call_count == 2


@patch("src.alerts.notifiers.email_delivery.requests.post")
@patch.dict(
    "os.environ",
    {
        "ALERT_EMAIL_PROVIDER": "mailgun",
        "MAILGUN_API_KEY": "mg-test-key",
        "MAILGUN_DOMAIN": "mg.markethelm.example",
        "ALERT_EMAIL_FROM": "alerts@markethelm.example",
        "ALERT_EMAIL_TO": "user@example.com",
    },
    clear=True,
)
def test_mailgun_request_exception_is_retriable(mock_post: MagicMock) -> None:
    mock_post.side_effect = requests.RequestException("connection reset")
    notifier = EmailNotifier.from_alert({"id": "a1", "notifications": ["email"]})
    assert notifier is not None
    with patch("src.alerts.notifiers.delivery_retry.time.sleep"):
        assert notifier.send({"alert_id": "a1", "alert_name": "Test", "symbols": []}) is False
    assert mock_post.call_count == 3


@patch.dict(
    "os.environ",
    {
        "ALERT_EMAIL_PROVIDER": "mailgun",
        "MAILGUN_DOMAIN": "mg.markethelm.example",
        "ALERT_EMAIL_FROM": "alerts@markethelm.example",
        "ALERT_EMAIL_TO": "user@example.com",
    },
    clear=True,
)
def test_mailgun_backend_requires_api_key() -> None:
    assert build_mailgun_backend() is None
    assert EmailNotifier.from_alert({"id": "a1", "notifications": ["email"]}) is None
    assert email_delivery_configured() is False


@patch.dict(
    "os.environ",
    {
        "ALERT_EMAIL_PROVIDER": "mailgun",
        "MAILGUN_API_KEY": "   ",
        "MAILGUN_DOMAIN": "mg.markethelm.example",
        "ALERT_EMAIL_FROM": "alerts@markethelm.example",
        "ALERT_EMAIL_TO": "user@example.com",
    },
    clear=True,
)
def test_mailgun_backend_rejects_blank_api_key() -> None:
    assert build_mailgun_backend() is None
    assert EmailNotifier.from_alert({"id": "a1", "notifications": ["email"]}) is None


@patch.dict(
    "os.environ",
    {
        "ALERT_EMAIL_PROVIDER": "mailgun",
        "MAILGUN_API_KEY": "mg-test-key",
        "ALERT_EMAIL_FROM": "alerts@markethelm.example",
        "ALERT_EMAIL_TO": "user@example.com",
    },
    clear=True,
)
def test_mailgun_backend_requires_domain() -> None:
    assert build_mailgun_backend() is None
    assert EmailNotifier.from_alert({"id": "a1", "notifications": ["email"]}) is None
    assert email_delivery_configured() is False


@patch.dict(
    "os.environ",
    {
        "ALERT_EMAIL_PROVIDER": "mailgun",
        "MAILGUN_API_KEY": "mg-test-key",
        "MAILGUN_DOMAIN": "  ",
        "ALERT_EMAIL_FROM": "alerts@markethelm.example",
        "ALERT_EMAIL_TO": "user@example.com",
    },
    clear=True,
)
def test_mailgun_backend_rejects_blank_domain() -> None:
    assert build_mailgun_backend() is None
    assert EmailNotifier.from_alert({"id": "a1", "notifications": ["email"]}) is None


@patch.dict(
    "os.environ",
    {
        "ALERT_EMAIL_PROVIDER": "sendgrid",
        "ALERT_EMAIL_FROM": "alerts@markethelm.example",
        "ALERT_EMAIL_TO": "user@example.com",
    },
    clear=True,
)
def test_sendgrid_backend_requires_api_key() -> None:
    assert build_sendgrid_backend() is None
    assert EmailNotifier.from_alert({"id": "a1", "notifications": ["email"]}) is None
    assert email_delivery_configured() is False


@patch.dict(
    "os.environ",
    {
        "ALERT_EMAIL_PROVIDER": "sendgrid",
        "SENDGRID_API_KEY": "  ",
        "ALERT_EMAIL_FROM": "alerts@markethelm.example",
        "ALERT_EMAIL_TO": "user@example.com",
    },
    clear=True,
)
def test_sendgrid_backend_rejects_blank_api_key() -> None:
    assert build_sendgrid_backend() is None
    assert EmailNotifier.from_alert({"id": "a1", "notifications": ["email"]}) is None


@patch.dict(
    "os.environ",
    {
        "SMTP_HOST": "smtp.example.com",
        "ALERT_EMAIL_FROM": "alerts@markethelm.example",
        "ALERT_EMAIL_TO": "user@example.com",
    },
    clear=True,
)
def test_smtp_backend_requires_credentials() -> None:
    assert build_smtp_backend({}) is None
    assert EmailNotifier.from_alert({"id": "a1", "notifications": ["email"]}) is None


@patch.dict(
    "os.environ",
    {
        "ALERT_EMAIL_PROVIDER": "mailgun",
        "MAILGUN_API_KEY": "mg-test-key",
        "MAILGUN_DOMAIN": "mg.markethelm.example",
        "MAILGUN_API_BASE": "https://api.eu.mailgun.net",
        "ALERT_EMAIL_FROM": "alerts@markethelm.example",
        "ALERT_EMAIL_TO": "user@example.com",
    },
    clear=True,
)
def test_mailgun_eu_api_base() -> None:
    backend = MailgunEmailBackend(
        "mg-test-key", "mg.markethelm.example", "https://api.eu.mailgun.net"
    )
    assert backend._api_base == "https://api.eu.mailgun.net"


@patch.dict(
    "os.environ",
    {
        "ALERT_EMAIL_PROVIDER": "ses",
        "SENDGRID_API_KEY": "sg-test-key",
        "ALERT_EMAIL_FROM": "alerts@markethelm.example",
    },
    clear=True,
)
def test_unknown_provider_falls_back_to_sendgrid_secret() -> None:
    assert resolve_email_provider() == "sendgrid"
    assert email_delivery_configured() is True


@patch.dict(
    "os.environ",
    {
        "ALERT_EMAIL_PROVIDER": "sendgrid",
        "SENDGRID_API_KEY": "sg-test-key",
    },
    clear=True,
)
def test_sendgrid_not_configured_without_from_address() -> None:
    assert email_delivery_configured() is False


@patch.dict(
    "os.environ",
    {
        "SMTP_HOST": "smtp.example.com",
        "SMTP_USER": "user@example.com",
        "SMTP_PASSWORD": "secret",
        "SMTP_PORT": "not-a-port",
    },
    clear=True,
)
def test_smtp_backend_rejects_invalid_port() -> None:
    assert build_smtp_backend({}) is None


@patch.dict(
    "os.environ",
    {
        "SMTP_HOST": "smtp.example.com",
        "SMTP_USER": "user@example.com",
        "SMTP_PASSWORD": "secret",
        "SMTP_PORT": "465",
        "SMTP_USE_SSL": "true",
    },
    clear=True,
)
def test_smtp_backend_ssl_on_465() -> None:
    backend = build_smtp_backend({})
    assert backend is not None
    assert backend._use_ssl is True
    assert backend._use_tls is False


@patch("src.alerts.notifiers.email_delivery.requests.post")
@patch.dict(
    "os.environ",
    {
        "ALERT_EMAIL_PROVIDER": "sendgrid",
        "SENDGRID_API_KEY": "sg-test-key",
        "ALERT_EMAIL_FROM": "alerts@markethelm.example",
        "ALERT_EMAIL_TO": "user@example.com",
    },
    clear=True,
)
def test_sendgrid_request_exception_is_retriable(mock_post: MagicMock) -> None:
    mock_post.side_effect = requests.RequestException("connection reset")
    notifier = EmailNotifier.from_alert({"id": "a1", "notifications": ["email"]})
    assert notifier is not None
    with patch("src.alerts.notifiers.delivery_retry.time.sleep"):
        assert notifier.send({"alert_id": "a1", "alert_name": "Test", "symbols": []}) is False
    assert mock_post.call_count == 3
