"""Synthesize a :class:`SelectorRecipe` from a page's HTML — the LLM step.

This is the one place scrapewright spends model tokens, and it spends them
**once per site**: the recipe it produces is cached and replayed
deterministically (see :mod:`scrapewright.extract.selectors`). So the cost of
onboarding a brand-new custom-HTML source is a single Claude call, and every
page after that is free.

The network call and the parsing are deliberately split: :func:`recipe_from_text`
is pure and unit-tested offline, while :meth:`LlmExtractor.synthesize` is the
thin wrapper that talks to the Anthropic API.
"""

from __future__ import annotations

import json
import re

from bs4 import BeautifulSoup

from .base import SelectorRecipe

# The Anthropic SDK is an optional extra — the deterministic Shopify / Woo /
# JSON-LD paths work without it. Import lazily so `import scrapewright` never
# hard-requires it.
DEFAULT_MODEL = "claude-opus-5"

_FIELDS = ("title", "price", "brand", "images", "description", "sku")

_PROMPT = """You are writing a reusable extractor for one e-commerce product page.

Given the HTML of a single product page, return CSS selectors that locate each
field. Return ONLY a JSON object, no prose, with this exact shape:

{{
  "title":       "<css selector or null>",
  "price":       "<css selector or null>",
  "brand":       "<css selector or null>",
  "images":      "<css selector matching <img> or <source> tags or null>",
  "description": "<css selector or null>",
  "sku":         "<css selector or null>",
  "modes": {{
     "<field>": "text" | "attr:<name>"
  }}
}}

Rules:
- Prefer stable selectors (ids, itemprop, data- attributes, semantic classes)
  over brittle nth-child chains.
- For a field whose value lives in an attribute (e.g. a meta tag's content, or
  an image's src), set its mode to "attr:<name>" — otherwise omit it (defaults
  to "text").
- "images" should match the gallery <img> elements; its mode is usually
  "attr:src".
- Use null for any field the page does not expose.

HTML:
```
{html}
```"""


def reduce_html(html: str, cap: int = 14000) -> str:
    """Strip script/style/noise and collapse whitespace so a product page
    fits comfortably in one prompt."""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "svg", "noscript"]):
        tag.decompose()
    text = str(soup)
    text = re.sub(r"\s+", " ", text)
    return text[:cap]


def recipe_from_text(text: str, origin: str = "") -> SelectorRecipe | None:
    """Parse a model's reply into a :class:`SelectorRecipe`. Pure — no network.

    Tolerates the reply being wrapped in ``` fences or surrounded by stray prose.
    """
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None

    fields = {f: (data.get(f) or None) for f in _FIELDS}
    modes = data.get("modes") or {}
    if not isinstance(modes, dict):
        modes = {}
    if not any(fields.values()):
        return None
    return SelectorRecipe(**fields, modes={str(k): str(v) for k, v in modes.items()},
                          origin=origin)


class LlmExtractor:
    """Wraps the Anthropic call that turns HTML into a recipe."""

    def __init__(self, model: str = DEFAULT_MODEL, api_key: str | None = None,
                 client=None):
        self.model = model
        self._client = client
        self._api_key = api_key

    def _get_client(self):
        if self._client is not None:
            return self._client
        try:
            import anthropic
        except ImportError as e:  # pragma: no cover - env dependent
            raise RuntimeError(
                "The LLM extractor needs the 'anthropic' package. "
                "Install it with:  pip install scrapewright[llm]"
            ) from e
        self._client = anthropic.Anthropic(api_key=self._api_key)
        return self._client

    def synthesize(self, html: str, url: str) -> SelectorRecipe | None:
        """Ask Claude for a recipe covering this page. One call per site."""
        prompt = _PROMPT.format(html=reduce_html(html))
        client = self._get_client()
        msg = client.messages.create(
            model=self.model,
            max_tokens=1500,
            messages=[{"role": "user", "content": prompt}],
        )
        text = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
        return recipe_from_text(text, origin=f"llm:{self.model}")
