"""The notes pages: reachable, and holding nothing they should not.

Publishing is the one action that cannot be taken back, so what goes on a
public page is checked by a test rather than by remembering. These pages
are written about real bugs in a real service, which is exactly the kind
of writing that leaks a hostname, a key, or somebody's address by
accident.
"""
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from scrapewright.service.app import create_app
from scrapewright.service.jobs import JobRegistry
from scrapewright.service.store import Store

STATIC = Path(__file__).resolve().parents[1] / "scrapewright" / "service" / "static"
POSTS = sorted(STATIC.glob("blog*.html"))

PATHS = ["/blog", "/blog/robots-txt", "/blog/a-sold-item-does-not-404",
         "/blog/never-bill-in-a-finally-block"]


@pytest.fixture()
def client(tmp_path):
    app = create_app(store=Store(str(tmp_path / "s.db")), jobs=JobRegistry())
    with TestClient(app) as c:
        yield c


def test_there_are_posts_to_check():
    assert len(POSTS) >= 4          # the index plus three articles


@pytest.mark.parametrize("path", PATHS)
def test_every_post_is_served(client, path):
    r = client.get(path)
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]


@pytest.mark.parametrize("path", PATHS)
def test_posts_carry_the_same_headers_as_the_rest_of_the_site(client, path):
    r = client.get(path)
    assert r.headers["Cache-Control"] == "no-cache"
    assert "frame-ancestors 'none'" in r.headers["Content-Security-Policy"]
    assert r.headers["X-Content-Type-Options"] == "nosniff"


def test_the_index_links_to_every_post():
    index = (STATIC / "blog.html").read_text(encoding="utf-8")
    for path in PATHS[1:]:
        assert f'href="{path}"' in index, path


# ── what must not be on a public page ───────────────────────────────────────
SECRETS = [
    (r"sk_live_|sk_test_|pk_live_", "a Stripe key"),
    (r"sw_[A-Za-z0-9_-]{16,}", "an API key"),
    (r"sk-ant-", "a model key"),
    (r"\.flycast|\.internal\b|fly\.dev", "an internal address"),
    (r"scrapewright-render|scrapewright-api", "an internal app name"),
    (r"RENDER_TOKEN|FLY_API_TOKEN|STRIPE_SECRET", "a secret's name"),
    (r"Z2250042A|Carritxal", "a home address or national id"),
    (r"fcbaye69", "a personal email"),
    (r"/data/|scrapewright\.db", "a server path"),
]


@pytest.mark.parametrize("post", POSTS, ids=lambda p: p.name)
@pytest.mark.parametrize("pattern,what", SECRETS, ids=lambda v: str(v)[:24])
def test_no_post_leaks(post, pattern, what):
    text = post.read_text(encoding="utf-8")
    found = re.search(pattern, text, re.IGNORECASE)
    assert not found, f"{post.name} contains {what}: {found.group(0)!r}"


@pytest.mark.parametrize("post", POSTS, ids=lambda p: p.name)
def test_no_post_loads_anything_from_anywhere_else(post):
    """The CSP on these pages forbids it, so a stray CDN link would not
    load -- it would just break the page silently.

    A canonical link is not a load: it names the page's own address for
    search engines and fetches nothing.
    """
    text = post.read_text(encoding="utf-8")
    for tag in re.findall(r'<(?:script|link|img)[^>]*>', text, re.IGNORECASE):
        if 'rel="canonical"' in tag:
            continue
        for url in re.findall(r'(?:src|href)="([^"]+)"', tag):
            assert not url.startswith("http"), f"{post.name} loads {url}"
