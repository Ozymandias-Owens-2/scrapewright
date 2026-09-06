"""Refusing to be used as a proxy into places the caller cannot reach.

The service fetches whatever URL it is given and a key costs nothing, so
without this anyone on the internet can point our server at the loopback
interface or a private network. /v1/extract returns the page it fetched, which
turns a port scan into a read. Verified exploitable against production before
this existed: detect happily reported on http://localhost:8000/health.
"""

import pytest

from scrapewright.safeurl import UnsafeUrl, check_url


@pytest.mark.parametrize("url", [
    "https://example.com/",
    "http://example.com/products",
    "https://sub.domain.example.co.uk/a/b?c=d",
])
def test_ordinary_sites_are_untouched(url):
    check_url(url)


@pytest.mark.parametrize("url", [
    "http://localhost:8000/health",
    "http://127.0.0.1/",
    "http://[::1]/",
    "http://10.0.0.5/",
    "http://192.168.1.1/",
    "http://172.16.0.1/",
])
def test_private_and_loopback_are_refused(url):
    with pytest.raises(UnsafeUrl):
        check_url(url)


def test_the_metadata_address_is_refused():
    """Link-local is where cloud providers keep credentials."""
    with pytest.raises(UnsafeUrl):
        check_url("http://169.254.169.254/latest/meta-data/")


@pytest.mark.parametrize("url", [
    "http://0177.0.0.1/",        # octal
    "http://2130706433/",        # the address as one integer
    "http://127.1/",             # short form
    "http://[::ffff:127.0.0.1]/",  # v4 wearing a v6 costume
])
def test_the_documented_bypasses_are_refused(url):
    """Every one of these is 127.0.0.1, and every one is in the cheat sheets.
    `ipaddress` rejects them as literals while the resolver beneath a Linux
    container accepts them, so a dotted-decimal-only check has a hole."""
    with pytest.raises(UnsafeUrl):
        check_url(url)


@pytest.mark.parametrize("url", [
    "file:///etc/passwd",
    "ftp://example.com/x",
    "gopher://example.com/",
])
def test_only_http_is_fetched(url):
    """file:// reads the container's own disk."""
    with pytest.raises(UnsafeUrl):
        check_url(url)


def test_a_url_with_no_host_is_refused():
    with pytest.raises(UnsafeUrl):
        check_url("http:///nowhere")


def test_a_name_resolving_to_a_private_address_is_refused(monkeypatch):
    """The usual attack is a public name pointed at a private address."""
    import scrapewright.safeurl as safeurl

    monkeypatch.setattr(safeurl, "resolved_addresses",
                        lambda host, port=None: ["93.184.216.34", "127.0.0.1"])

    # One bad answer out of two is enough: a name can resolve to several.
    with pytest.raises(UnsafeUrl):
        check_url("https://sneaky.example.com/")


def test_self_hosting_against_an_internal_site_can_be_allowed(monkeypatch):
    """Crawling your own intranet is legitimate. Being the default is not."""
    monkeypatch.setenv("SCRAPEWRIGHT_ALLOW_PRIVATE", "1")

    check_url("http://127.0.0.1:8000/")


def test_it_is_a_request_exception():
    """So callers already treating a failed fetch as "no content" degrade
    safely instead of returning a traceback to a customer."""
    import requests

    assert issubclass(UnsafeUrl, requests.RequestException)
