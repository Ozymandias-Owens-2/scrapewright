"""scrapewright — give it a store URL, it writes the scraper.

Public API:

    from scrapewright import Scrapewright, Product, detect

    sw = Scrapewright()
    for product in sw.scrape_catalog("https://shop.example.com"):
        print(product.title, product.price)

    one = sw.scrape_page("https://boutique.example.com/products/coat")
"""

from .cache import RecipeCache
from .crawl import Frontier
from .detect import Detection, detect
from .fetch import BrowserFetcher, StaticFetcher
from .models import Product, Record, parse_price
from .pipeline import Scrapewright, check
from .schema import PRODUCT_SCHEMA, Field, Schema
from .validate import Coverage, coverage

__version__ = "0.7.0"

__all__ = [
    "Scrapewright",
    "Frontier",
    "StaticFetcher",
    "BrowserFetcher",
    "Product",
    "Record",
    "Schema",
    "Field",
    "PRODUCT_SCHEMA",
    "Detection",
    "detect",
    "check",
    "coverage",
    "Coverage",
    "RecipeCache",
    "parse_price",
    "__version__",
]
