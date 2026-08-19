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
from .models import Product, parse_price
from .pipeline import Scrapewright, check
from .validate import Coverage, coverage

__version__ = "0.2.0"

__all__ = [
    "Scrapewright",
    "Frontier",
    "Product",
    "Detection",
    "detect",
    "check",
    "coverage",
    "Coverage",
    "RecipeCache",
    "parse_price",
    "__version__",
]
