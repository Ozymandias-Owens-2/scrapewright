"""Extractors: deterministic (shopify, woocommerce, json-ld, selector replay)
and LLM-synthesized (llm)."""

from .base import Extractor, SelectorRecipe
from .jsonld import JsonLdExtractor
from .llm import LlmExtractor, recipe_from_text
from .selectors import SelectorExtractor
from .shopify import ShopifyExtractor
from .woocommerce import WooCommerceExtractor

__all__ = [
    "Extractor",
    "SelectorRecipe",
    "JsonLdExtractor",
    "LlmExtractor",
    "recipe_from_text",
    "SelectorExtractor",
    "ShopifyExtractor",
    "WooCommerceExtractor",
]
