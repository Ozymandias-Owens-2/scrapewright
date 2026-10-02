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


# ── an error page is not a page ──────────────────────────────────────────────
class _Response:
    def __init__(self, status): self.status = status


class _Page:
    def __init__(self, status): self.status, self.content_calls = status, 0
    def goto(self, url, **kwargs): return _Response(self.status)
    def wait_for_timeout(self, ms): pass
    def content(self):
        self.content_calls += 1
        return "<html><body>404 not found</body></html>"


def _fetch_with(status, monkeypatch):
    from scrapewright.fetch import BrowserFetcher

    page = _Page(status)
    fetcher = BrowserFetcher()
    monkeypatch.setattr(fetcher, "_ensure_page", lambda: page)
    monkeypatch.setattr("scrapewright.safeurl.check_url", lambda url: None)
    monkeypatch.setattr("scrapewright.robots.check", lambda url: None)
    return fetcher.fetch("https://shop.test/gone"), page


def test_a_rendered_404_is_not_content(monkeypatch):
    """A browser renders an error page as happily as a real one. Without
    this a dead link in a spreadsheet looked like a page with no price, and
    every refresh paid a model to read "404 not found"."""
    html, page = _fetch_with(404, monkeypatch)
    assert html is None
    assert page.content_calls == 0


def test_a_rendered_200_is(monkeypatch):
    html, _ = _fetch_with(200, monkeypatch)
    assert html is not None


def test_the_static_fetcher_has_always_done_this():
    import inspect
    from scrapewright.fetch import StaticFetcher

    assert "status_code != 200" in inspect.getsource(StaticFetcher.fetch)
