from scrapewright.cache import RecipeCache, domain_of
from scrapewright.extract.base import SelectorRecipe
from scrapewright.models import Product
from scrapewright.validate import coverage


def test_domain_of_strips_www_and_scheme():
    assert domain_of("https://www.Antonioli.eu/products/x") == "antonioli.eu"
    assert domain_of("shop.example.com") == "shop.example.com"


def test_cache_roundtrip(tmp_path):
    cache = RecipeCache(tmp_path / "recipes.json")
    assert cache.get("https://shop.example.com/p") is None

    recipe = SelectorRecipe(title=".t", price=".p", modes={"images": "attr:src"})
    cache.put("https://www.shop.example.com/products/a", recipe)

    got = cache.get("https://shop.example.com/products/b")   # same domain, diff path
    assert got is not None
    assert got.title == ".t"
    assert cache.domains() == ["shop.example.com"]


def test_coverage_ratio():
    products = [
        Product(url="u1", title="A", price="10"),   # usable
        Product(url="u2", title="B", price="20"),   # usable
        Product(url="u3", title="C"),               # missing price
    ]
    cov = coverage(products)
    assert cov.total == 3
    assert cov.usable == 2
    assert round(cov.usable_ratio, 2) == 0.67
    assert cov.per_field["price"] == round(2 / 3, 3)
    assert cov.meets(0.5)
    assert not cov.meets(0.9)


def test_coverage_empty():
    cov = coverage([])
    assert cov.usable_ratio == 0.0
