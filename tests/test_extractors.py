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
