"""robots.txt, and the awkward parts of RFC 9309.

The easy case -- a Disallow line is obeyed -- is one test. The rest are the
cases people get backwards, where the wrong answer is invisible until someone
sends a complaint.
"""

import pytest
import requests

from scrapewright import robots
from scrapewright.fetch import StaticFetcher
from scrapewright.robots import RobotsDisallowed, RobotsPolicy


class FakeResponse:
    def __init__(self, status: int, text: str = ""):
        self.status_code, self.text = status, text


class FakeSession:
    """Serves one robots.txt and records what else was asked for."""

    def __init__(self, response: FakeResponse):
        self.response = response
        self.fetched: list[str] = []

    def get(self, url, **kw):
        self.fetched.append(url)
        if url.endswith("/robots.txt"):
            return self.response
        return FakeResponse(200, "<html>page</html>")


RULES = "User-agent: *\nDisallow: /private/\nCrawl-delay: 2\n"


@pytest.fixture(autouse=True)
def restore_policy():
    """Never let a test leak its policy into the next one."""
    before = robots.get_policy()
    yield
    robots.set_policy(before)


def test_a_disallowed_path_is_refused():
    policy = RobotsPolicy(session=FakeSession(FakeResponse(200, RULES)))

    assert policy.allows("https://shop.test/catalog")
    assert not policy.allows("https://shop.test/private/orders")


def test_no_robots_file_means_everything_is_allowed():
    """404 is absence, and absence is permission."""
    policy = RobotsPolicy(session=FakeSession(FakeResponse(404)))

    assert policy.allows("https://shop.test/anything")


def test_a_refused_robots_file_means_nothing_is_allowed():
    """401/403 is not absence -- the site is refusing us outright."""
    for status in (401, 403):
        policy = RobotsPolicy(session=FakeSession(FakeResponse(status)))
        assert not policy.allows("https://shop.test/anything"), status


def test_a_server_error_does_not_lock_us_out():
    """5xx says nothing either way; let the ordinary fetch fail instead."""
    policy = RobotsPolicy(session=FakeSession(FakeResponse(500)))

    assert policy.allows("https://shop.test/anything")


def test_robots_is_fetched_once_per_origin():
    session = FakeSession(FakeResponse(200, RULES))
    policy = RobotsPolicy(session=session)

    for i in range(5):
        policy.allows(f"https://shop.test/page/{i}")

    assert session.fetched.count("https://shop.test/robots.txt") == 1


def test_crawl_delay_is_reported():
    policy = RobotsPolicy(session=FakeSession(FakeResponse(200, RULES)))

    assert policy.crawl_delay("https://shop.test/catalog") == 2.0


def test_the_fetcher_declines_a_disallowed_url():
    """The integration that matters: a blocked URL yields no content."""
    robots.set_policy(RobotsPolicy(session=FakeSession(FakeResponse(200, RULES))))

    assert StaticFetcher().fetch("https://shop.test/private/orders") is None


def test_disallowed_reads_as_a_request_failure():
    """RobotsDisallowed is a RequestException, so old handling still works."""
    assert issubclass(RobotsDisallowed, requests.RequestException)


def test_checking_can_be_turned_off_deliberately():
    """Crawling your own site is legitimate; it just must not be the default."""
    robots.set_policy(RobotsPolicy(session=FakeSession(FakeResponse(403))))
    assert not robots.get_policy().allows("https://mine.test/x")

    robots.set_policy(None)
    robots.check("https://mine.test/x")   # must not raise


def test_the_user_agent_says_who_we_are():
    """robots.txt is addressed to a named agent; a browser disguise cannot be
    given permission by name."""
    from scrapewright.http import USER_AGENT

    assert USER_AGENT.startswith("scrapewright/")
    assert "Mozilla" not in USER_AGENT
    assert "github.com/" in USER_AGENT
