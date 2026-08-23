"""Schema-agnostic extraction: the same compile-once/replay-free loop, aimed
at whatever fields the caller declares."""

import pytest

from scrapewright.cache import RecipeCache, cache_key
from scrapewright.extract.base import SelectorRecipe
from scrapewright.extract.llm import recipe_from_text
from scrapewright.extract.selectors import SelectorExtractor
from scrapewright.pipeline import Scrapewright
from scrapewright.schema import PRODUCT_SCHEMA, Field, Schema
from tests.test_pipeline import _FakeSession

JOB_HTML = """
<html><body>
  <h1 class="posting-title">Backend Engineer</h1>
  <span class="company">Nordwind</span>
  <div class="salary">€72,000</div>
  <ul class="tags"><li class="tag">python</li><li class="tag">postgres</li></ul>
</body></html>
"""

JOB_SCHEMA = Schema(
    name="job",
    fields=(
        Field("title", "the job title"),
        Field("company", "the hiring company"),
        Field("salary", "the advertised salary", "number"),
        Field("tags", "the listed skills", "list"),
    ),
    required=("title", "company"),
)

JOB_RECIPE = SelectorRecipe(
    fields={"title": ".posting-title", "company": ".company",
            "salary": ".salary", "tags": ".tag"},
    modes={"tags": "attr_all:text"},
    schema_name="job",
)


class _FakeSchemaLLM:
    def __init__(self, recipe):
        self.recipe = recipe
        self.schemas_seen = []

    def synthesize(self, html, url, schema=None):
        self.schemas_seen.append(schema)
        return self.recipe


# ── schema declaration ───────────────────────────────────────────────────────
def test_from_names_parses_kinds():
    s = Schema.from_names(["title", "salary:number", "tags:list"], name="job")
    assert s.field_names == ("title", "salary", "tags")
    assert s.list_fields == {"tags"}
    assert s.required == ("title",)          # first field by default


def test_rejects_unknown_kind():
    with pytest.raises(ValueError):
        Field("x", kind="octopus")


def test_is_satisfied_by():
    assert JOB_SCHEMA.is_satisfied_by({"title": "A", "company": "B"})
    assert not JOB_SCHEMA.is_satisfied_by({"title": "A"})


# ── replay against a non-product schema ──────────────────────────────────────
def test_selector_replay_with_custom_fields():
    record = SelectorExtractor(JOB_RECIPE, JOB_SCHEMA).extract_record(
        JOB_HTML, "https://jobs.example.com/p/1")
    assert record is not None
    assert record.schema_name == "job"
    assert record.data["title"] == "Backend Engineer"
    assert record.data["company"] == "Nordwind"
    assert record.data["salary"] == "€72,000"
    assert record.data["tags"] == ["python", "postgres"]


def test_pipeline_extract_with_custom_schema(tmp_path):
    cache = RecipeCache(tmp_path / "r.json")
    llm = _FakeSchemaLLM(JOB_RECIPE)
    sw = Scrapewright(cache=cache, llm=llm, session=_FakeSession(JOB_HTML))
    url = "https://jobs.example.com/p/1"

    record = sw.extract(url, JOB_SCHEMA)
    assert record is not None and record.data["company"] == "Nordwind"
    # The schema reached the model, so the prompt asked for the right fields.
    assert llm.schemas_seen == [JOB_SCHEMA]

    # Second call replays from cache — no further synthesis.
    again = sw.extract(url, JOB_SCHEMA)
    assert again is not None and again.data["title"] == "Backend Engineer"
    assert len(llm.schemas_seen) == 1


def test_product_schema_still_returns_typed_products(tmp_path, fixture):
    """The typed path is unchanged by the generalization."""
    sw = Scrapewright(cache=RecipeCache(tmp_path / "r.json"),
                      session=_FakeSession(fixture("jsonld_product.html")))
    product = sw.scrape_page("https://maison.example.com/overcoat")
    assert product is not None and product.title == "Wool Overcoat"
    assert product.is_usable()


# ── cache isolation between schemas ──────────────────────────────────────────
def test_cache_keys_separate_schemas(tmp_path):
    cache = RecipeCache(tmp_path / "r.json")
    url = "https://site.example.com/x"
    cache.put(url, SelectorRecipe(title=".p-title"), "product")
    cache.put(url, JOB_RECIPE, "job")

    assert cache.get(url, "product").title == ".p-title"
    assert cache.get(url, "job").fields["company"] == ".company"
    assert cache_key(url) == "site.example.com"          # product stays bare
    assert cache_key(url, "job") == "site.example.com#job"


def test_legacy_cache_file_still_loads(tmp_path):
    """A recipes.json written by v0.3 (flat field keys) must keep working."""
    path = tmp_path / "r.json"
    path.write_text('{"old.example.com": {"title": ".t", "price": ".p", '
                    '"modes": {}, "needs_js": false, "origin": "llm:x"}}',
                    encoding="utf-8")
    recipe = RecipeCache(path).get("https://old.example.com/a")
    assert recipe is not None
    assert recipe.title == ".t" and recipe.price == ".p"
    assert recipe.fields == {"title": ".t", "price": ".p"}


# ── model reply parsing ──────────────────────────────────────────────────────
def test_recipe_from_text_accepts_nested_fields_shape():
    reply = '{"fields": {"title": "h1", "salary": ".pay"}, "modes": {"salary": "text"}}'
    recipe = recipe_from_text(reply, schema_name="job")
    assert recipe is not None
    assert recipe.fields == {"title": "h1", "salary": ".pay"}
    assert recipe.schema_name == "job"


def test_recipe_from_text_drops_null_selectors():
    reply = '{"fields": {"title": "h1", "sku": null}}'
    recipe = recipe_from_text(reply)
    assert recipe is not None and recipe.fields == {"title": "h1"}
