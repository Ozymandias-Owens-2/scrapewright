"""Two field sets are two schemas, even when neither was given a name.

Recipes are cached per domain and schema name. While every unnamed schema
was called `custom`, the second caller to compile a site with different
fields replayed the first caller's recipe — across customers, since the
cache is shared.
"""
from scrapewright.cache import cache_key
from scrapewright.schema import Schema


def test_different_field_sets_get_different_names():
    a = Schema.from_names(["title", "price:number"])
    b = Schema.from_names(["title", "brand", "location"])
    assert a.name != b.name


def test_the_same_field_set_is_stable_across_calls():
    assert (Schema.from_names(["title", "price:number"]).name
            == Schema.from_names(["title", "price:number"]).name)


def test_a_kind_change_is_a_different_schema():
    assert (Schema.from_names(["title", "price"]).name
            != Schema.from_names(["title", "price:number"]).name)


def test_an_explicit_name_is_left_alone():
    assert Schema.from_names(["title"], name="job").name == "job"


def test_the_cache_keys_diverge_for_one_domain():
    a = Schema.from_names(["title", "price:number"])
    b = Schema.from_names(["title", "brand"])
    assert (cache_key("https://shop.test/x", a.name)
            != cache_key("https://shop.test/x", b.name))
