from decimal import Decimal

from scrapewright.extract.base import SelectorRecipe
from scrapewright.extract.jsonld import JsonLdExtractor
from scrapewright.extract.selectors import SelectorExtractor
from scrapewright.extract.shopify import ShopifyExtractor
from scrapewright.extract.woocommerce import WooCommerceExtractor

SHOPIFY_PAYLOAD = {
    "products": [
        {
            "handle": "belas-trousers",
            "title": "Belas Trousers",
            "vendor": "Rick Owens",
            "body_html": "<p>Cotton poplin trousers.</p>",
            "variants": [
                {"title": "IT 48", "price": "890.00", "available": True, "sku": "RO-BELAS-48"},
                {"title": "IT 50", "price": "890.00", "available": False},
            ],
            "images": [{"src": "https://cdn.shopify.com/belas-1.jpg"}],
        }
    ]
}

WOO_PAYLOAD = [
    {
        "name": "Field Jacket",
        "permalink": "https://shop.example.com/product/field-jacket",
        "sku": "FJ-01",
        "short_description": "<p>Slim field jacket.</p>",
        "prices": {"price": "24500", "currency_code": "EUR", "currency_minor_unit": 2},
        "images": [{"src": "https://shop.example.com/fj.jpg"}],
        "is_in_stock": True,
    }
]


def test_shopify_mapping():
    ex = ShopifyExtractor("https://shop.example.com/products.json")
    products = ex.products_from_payload(SHOPIFY_PAYLOAD)
    assert len(products) == 1
    p = products[0]
    assert p.title == "Belas Trousers"
    assert p.brand == "Rick Owens"
    assert p.price == Decimal("890.00")
    assert p.available is True                       # at least one variant available
    assert p.sizes == ["IT 48", "IT 50"]
    assert p.url == "https://shop.example.com/products/belas-trousers"
    assert p.images == ["https://cdn.shopify.com/belas-1.jpg"]
    assert p.description == "Cotton poplin trousers."
    assert p.is_usable()


def test_woocommerce_minor_units():
    ex = WooCommerceExtractor("https://shop.example.com/wp-json/wc/store/products")
    p = ex.products_from_payload(WOO_PAYLOAD)[0]
    assert p.price == Decimal("245.00")              # 24500 cents / 10**2
    assert p.currency == "EUR"
    assert p.available is True
    assert p.description == "Slim field jacket."


def test_jsonld_extraction(fixture):
    p = JsonLdExtractor().extract_page(fixture("jsonld_product.html"), "https://x/overcoat")
    assert p is not None
    assert p.title == "Wool Overcoat"
    assert p.brand == "Maison Example"
    assert p.price == Decimal("1290.00")
    assert p.currency == "EUR"
    assert p.available is True
    assert p.sku == "MO-113-BLK"
    assert len(p.images) == 2
    assert p.is_usable()


def test_selector_replay_on_generic_html(fixture):
    recipe = SelectorRecipe(
        title=".product__title",
        brand=".brand",
        price=".price",
        description=".description",
        sku=".sku-code",
        images=".gallery__img",
        modes={"images": "attr:src"},
    )
    p = SelectorExtractor(recipe).extract_page(
        fixture("generic_product.html"),
        "https://boutique.example.com/products/ricki",
    )
    assert p is not None
    assert p.title == "Ricki Leather Boots"
    assert p.brand == "Deadwood"
    assert p.price == Decimal("380")                 # coerced from "€380"
    assert p.sku == "DW-RICKI-42"
    # relative image srcs resolved against the page URL
    assert p.images == [
        "https://boutique.example.com/img/boot-front.jpg",
        "https://boutique.example.com/img/boot-side.jpg",
    ]
    assert p.is_usable()


# ── list fields beyond images ────────────────────────────────────────────────
def test_a_list_of_table_cells_is_read_as_text():
    """List fields defaulted to reading `src`, so anything that was not an
    image came back empty and the whole record collapsed to None. Found on a
    Treasury data table: the model wrote a correct recipe and got nothing."""
    from scrapewright.extract.base import SelectorRecipe
    from scrapewright.extract.selectors import SelectorExtractor
    from scrapewright.schema import Schema

    html = """<table><tbody>
      <tr data-testid="row"><td>9/30/2010</td><td>$414 B</td></tr>
      <tr data-testid="row"><td>9/30/2011</td><td>$454.4 B</td></tr>
    </tbody></table>"""
    schema = Schema.from_names(["date:list", "spend:list"], name="rates")
    recipe = SelectorRecipe(origin="https://t.test", schema_name="rates", fields={
        "date": 'tbody tr[data-testid="row"] td:nth-child(1)',
        "spend": 'tbody tr[data-testid="row"] td:nth-child(2)',
    })

    values = SelectorExtractor(recipe, schema).extract_values(html, "https://t.test")

    assert values["date"] == ["9/30/2010", "9/30/2011"]
    assert values["spend"] == ["$414 B", "$454.4 B"]


def test_a_list_of_images_still_reads_src():
    """The fix must not break what the old default was there for."""
    from scrapewright.extract.base import SelectorRecipe
    from scrapewright.extract.selectors import SelectorExtractor
    from scrapewright.schema import Schema

    html = '<div class="g"><img src="/a.jpg"><img src="/b.jpg"></div>'
    schema = Schema.from_names(["images:list"], name="product")
    recipe = SelectorRecipe(origin="https://s.test", schema_name="product",
                            fields={"images": ".g img"})

    values = SelectorExtractor(recipe, schema).extract_values(html, "https://s.test/p")

    assert values["images"] == ["https://s.test/a.jpg", "https://s.test/b.jpg"]


def test_a_list_of_links_reads_href():
    from scrapewright.extract.base import SelectorRecipe
    from scrapewright.extract.selectors import SelectorExtractor
    from scrapewright.schema import Schema

    html = '<ul><li><a href="/one">One</a></li><li><a href="/two">Two</a></li></ul>'
    schema = Schema.from_names(["links:list"], name="page")
    recipe = SelectorRecipe(origin="https://s.test", schema_name="page",
                            fields={"links": "ul a"})

    values = SelectorExtractor(recipe, schema).extract_values(html, "https://s.test/")

    assert values["links"] == ["https://s.test/one", "https://s.test/two"]


def test_an_explicit_mode_still_wins():
    """A recipe that names the attribute must be obeyed over the guess."""
    from scrapewright.extract.base import SelectorRecipe
    from scrapewright.extract.selectors import SelectorExtractor
    from scrapewright.schema import Schema

    html = '<div><span data-value="7">seven</span><span data-value="8">eight</span></div>'
    schema = Schema.from_names(["nums:list"], name="page")
    recipe = SelectorRecipe(origin="https://s.test", schema_name="page",
                            fields={"nums": "span"},
                            modes={"nums": "attr_all:data-value"})

    values = SelectorExtractor(recipe, schema).extract_values(html, "https://s.test/")

    assert values["nums"] == ["7", "8"]
