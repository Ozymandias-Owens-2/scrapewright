"""The stopping rule for infinite scroll.

Half of modern shops load more items as you scroll, so a single render sees a
twenty-item slice of a two-hundred-item catalogue. The difficulty is not the
scrolling, it is knowing when to stop: too early and the catalogue is
truncated, never and one page consumes the whole job.
"""

from scrapewright.fetch import scroll_to_end


class FakePage:
    """A page whose height grows for a while and then does not."""

    def __init__(self, heights):
        self.heights = list(heights)
        self.scrolls = 0
        self.waits = 0

    def evaluate(self, script):
        if "scrollTo" in script:
            self.scrolls += 1
            return None
        return self.heights.pop(0) if self.heights else 0

    def wait_for_timeout(self, ms):
        self.waits += 1


def test_it_stops_when_the_page_stops_growing():
    """The honest signal that everything has loaded."""
    page = FakePage([1000, 2000, 3000, 3000])

    rounds = scroll_to_end(page, max_scrolls=20, pause_ms=0)

    assert rounds == 3
    assert page.scrolls == 3          # not twenty


def test_a_page_that_never_grows_costs_one_round():
    page = FakePage([500, 500])

    assert scroll_to_end(page, max_scrolls=20, pause_ms=0) == 1


def test_an_endless_feed_is_capped():
    """Some feeds genuinely never end. The job must still finish."""
    page = FakePage([1000 * i for i in range(1, 100)])

    rounds = scroll_to_end(page, max_scrolls=5, pause_ms=0)

    assert rounds == 5
    assert page.scrolls == 5


def test_a_shrinking_page_stops_rather_than_looping():
    """Some sites unload offscreen items. Treat that as done, not as growth."""
    page = FakePage([3000, 2000])

    assert scroll_to_end(page, max_scrolls=20, pause_ms=0) == 1


def test_scrolling_is_off_unless_asked_for():
    """Scrolling a page that does not grow is time nobody is paying for."""
    from scrapewright.fetch import BrowserFetcher

    assert BrowserFetcher().max_scrolls == 0


def test_the_pipeline_hands_the_setting_to_the_browser():
    """It was dead code until something passed it through."""
    from scrapewright.pipeline import Scrapewright

    sw = Scrapewright(js=True, max_scrolls=7)
    browser = sw._get_browser()

    assert browser.max_scrolls == 7
    sw.close()


def test_the_crawl_endpoint_accepts_it_and_defaults_to_off():
    from scrapewright.service.app import CrawlRequest

    assert CrawlRequest(url="https://x.test").scroll == 0
    assert CrawlRequest(url="https://x.test", scroll=10).scroll == 10


def test_an_absurd_scroll_count_is_refused():
    """A caller must not be able to make one page run for an hour."""
    import pytest
    from pydantic import ValidationError

    from scrapewright.service.app import CrawlRequest

    with pytest.raises(ValidationError):
        CrawlRequest(url="https://x.test", scroll=5000)
