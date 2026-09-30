"""A field that is filled on some pages and empty on others is a second
layout, not an absence — and the recipe can learn it.

Offline: the "model" is a stub that returns the sidebar selector.
"""
from scrapewright.cache import RecipeCache
from scrapewright.extract.base import SelectorRecipe
from scrapewright.extract.selectors import SelectorExtractor
from scrapewright.pipeline import Scrapewright
from scrapewright.schema import Schema

SCHEMA = Schema.from_names(["title", "salary"])

BANNER = """<html><body><h1>{t}</h1>
  <div class="pay-banner"><span class="pay-range">$90,000 USD</span></div>
  {links}</body></html>"""
SIDEBAR = """<html><body><h1>{t}</h1>
  <ul class="about"><li class="pay">Salary $75,000 USD</li></ul>
  {links}</body></html>"""

RECIPE = SelectorRecipe(fields={"title": "h1", "salary": ".pay-banner .pay-range"})


def test_alternates_are_tried_in_order_after_the_first():
    recipe = RECIPE.model_copy(deep=True)
    recipe.add_alternate("salary", ".about .pay")
    got = SelectorExtractor(recipe, SCHEMA).extract_values(
        SIDEBAR.format(t="A", links=""), "https://jobs.test/a")
    assert got["salary"] == "Salary $75,000 USD"
    # The learned selector still wins where it matches.
    got = SelectorExtractor(recipe, SCHEMA).extract_values(
        BANNER.format(t="B", links=""), "https://jobs.test/b")
    assert got["salary"] == "$90,000 USD"


def test_a_duplicate_alternate_is_not_added_twice():
    recipe = RECIPE.model_copy(deep=True)
    assert recipe.add_alternate("salary", ".about .pay") is True
    assert recipe.add_alternate("salary", ".about .pay") is False
    assert recipe.add_alternate("salary", ".pay-banner .pay-range") is False


class _Llm:
    """Compiles the banner layout first, then answers the repair call."""
    def __init__(self): self.calls = []

    def synthesize(self, html, url, schema=None, **kw):
        self.calls.append(tuple(schema.field_names) if schema else ())
        if "pay-banner" in html and len(self.calls) == 1:
            return RECIPE.model_copy(deep=True)
        return SelectorRecipe(fields={"salary": ".about .pay"})


class _Site:
    """One banner page linking to four sidebar pages."""
    def __init__(self):
        links = "".join(f'<a href="/jobs/s{i}">s{i}</a>' for i in range(4))
        self.pages = {"https://jobs.test/jobs/first": BANNER.format(t="first", links=links)}
        for i in range(4):
            self.pages[f"https://jobs.test/jobs/s{i}"] = SIDEBAR.format(t=f"s{i}", links="")

    def fetch(self, url):
        return self.pages.get(url)


def test_the_run_repairs_itself_with_one_extra_call(tmp_path, monkeypatch):
    monkeypatch.setattr("scrapewright.like._sitemap_urls",
                        lambda origin, session=None: [])
    monkeypatch.setattr("scrapewright.like._text_of", lambda url, session=None: None)

    llm = _Llm()
    sw = Scrapewright(cache=RecipeCache(tmp_path / "r.json"), llm=llm,
                      fetcher=_Site())
    rows = list(sw.crawl_like("https://jobs.test/jobs/first", SCHEMA, max_items=10))

    assert len(rows) == 5
    # Every row carries a salary, including the four the first recipe missed.
    assert all(r.data.get("salary") for r in rows), [r.data for r in rows]
    # Exactly two model calls for the whole site: the compile and the repair,
    # and the repair asked only about the field that was missing.
    assert llm.calls == [("title", "salary"), ("salary",)]


def test_a_field_missing_everywhere_is_not_worth_a_call(tmp_path, monkeypatch):
    """Most postings really have no salary. Paying to rediscover that on
    every site would be worse than the hole."""
    monkeypatch.setattr("scrapewright.like._sitemap_urls",
                        lambda origin, session=None: [])
    monkeypatch.setattr("scrapewright.like._text_of", lambda url, session=None: None)

    bare = "<html><body><h1>{t}</h1>{links}</body></html>"

    class _Bare:
        def __init__(self):
            links = "".join(f'<a href="/jobs/s{i}">s{i}</a>' for i in range(4))
            self.pages = {"https://jobs.test/jobs/first": bare.format(t="first", links=links)}
            for i in range(4):
                self.pages[f"https://jobs.test/jobs/s{i}"] = bare.format(t=f"s{i}", links="")
        def fetch(self, url): return self.pages.get(url)

    class _TitleOnly:
        def __init__(self): self.calls = 0
        def synthesize(self, html, url, schema=None, **kw):
            self.calls += 1
            return SelectorRecipe(fields={"title": "h1"})

    llm = _TitleOnly()
    sw = Scrapewright(cache=RecipeCache(tmp_path / "r.json"), llm=llm, fetcher=_Bare())
    rows = list(sw.crawl_like("https://jobs.test/jobs/first", SCHEMA, max_items=10))
    assert len(rows) == 5
    assert llm.calls == 1          # compiled once, never repaired
