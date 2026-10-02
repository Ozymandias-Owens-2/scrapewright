"""Chromium opens pages chosen by strangers. It does not need our keys."""
from scrapewright.fetch import _BROWSER_ENV_KEEP, _browser_env


def test_secrets_do_not_reach_the_browser(monkeypatch):
    for name in ("STRIPE_SECRET_KEY", "ANTHROPIC_API_KEY", "AWS_SECRET_ACCESS_KEY",
                 "STRIPE_WEBHOOK_SECRET", "BUCKET_NAME"):
        monkeypatch.setenv(name, "secret-value")

    env = _browser_env()
    assert not any(value == "secret-value" for value in env.values())
    assert "STRIPE_SECRET_KEY" not in env


def test_what_it_needs_is_passed_through(monkeypatch):
    monkeypatch.setenv("PATH", "/usr/bin")
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", "/ms-playwright")
    env = _browser_env()
    assert env["PATH"] == "/usr/bin"
    assert env["PLAYWRIGHT_BROWSERS_PATH"] == "/ms-playwright"


def test_home_is_always_set(monkeypatch):
    """Chromium writes a profile somewhere; without HOME it picks badly."""
    monkeypatch.delenv("HOME", raising=False)
    assert _browser_env()["HOME"]


def test_the_keep_list_holds_nothing_secret():
    assert not any(
        word in name.upper()
        for name in _BROWSER_ENV_KEEP
        for word in ("KEY", "SECRET", "TOKEN", "PASSWORD", "BUCKET"))
