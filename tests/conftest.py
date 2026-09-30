from pathlib import Path

import pytest

from scrapewright import robots
from scrapewright.robots import RobotsPolicy

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def fixture():
    def _read(name: str) -> str:
        return (FIXTURES / name).read_text(encoding="utf-8")
    return _read


class _NoRobotsFile:
    """Answers every robots.txt with 404: no rules, so nothing is forbidden."""

    class _Response:
        status_code = 404
        text = ""

    def get(self, url, **kw):
        return self._Response()


@pytest.fixture(autouse=True)
def offline_robots():
    """Keep the suite from reaching for a real robots.txt.

    Since this module started obeying RFC 9309 §2.3.1.4 -- a robots.txt that
    cannot be reached means complete disallow -- a test host like
    ``shop.test`` fails to resolve and every fetch in the suite is, correctly,
    refused. Right in production, useless in a test. So the default policy
    here is one that finds no robots.txt anywhere.

    Tests about robots itself install their own policy and restore this one.
    """
    before = robots.get_policy()
    robots.set_policy(RobotsPolicy(session=_NoRobotsFile()))
    yield
    robots.set_policy(before)
