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
import os
import re

from bs4 import BeautifulSoup, Comment

from ..schema import PRODUCT_SCHEMA, Schema
from .base import SelectorRecipe

# The Anthropic SDK is an optional extra — the deterministic Shopify / Woo /
# JSON-LD paths work without it. Import lazily so `import scrapewright` never
# hard-requires it.
# Writing a CSS selector from a page is a structured job with a checkable
# answer, and the cheap model is good at it. Measured over twelve live pages
# from a customer's spreadsheet: Haiku got the price right on 9 of 9 readable
# pages, Opus on 8 -- Opus took the struck-through 50,00 EUR on a climbing
# shop where Haiku took the 100,00 EUR a buyer pays. Per compile: $0.0021
# against $0.1004, which is forty-eight times.
#
# Where Haiku is worse is in answering when it should not: offered no real
# availability text it will point at the specification table. That is caught
# at compile time now (see ``prune_unusable``), and anything it gets wrong
# enough to leave the page unanswered is asked again of the stronger model,
# below -- so a bad guess costs an Opus call, not a wrong number in a
# customer's spreadsheet.
DEFAULT_MODEL = os.environ.get("SCRAPEWRIGHT_SYNTH_MODEL", "claude-haiku-5-5")
# Asked only when the cheap recipe does not answer the page it was written
# from. Set the variable empty to turn escalation off.
ESCALATION_MODEL = os.environ.get("SCRAPEWRIGHT_SYNTH_ESCALATION",
                                  "claude-opus-5")


class ProviderError(RuntimeError):
    """The model provider refused the call. Ours to retry, not the caller's.

    Six synthesis calls in a row came back HTTP 400 one evening and the
    same six pages went through minutes later. Whatever that was, it was
    not a verdict on the page: billing for it, remembering it as a failure,
    or answering 500 would all turn a passing cloud problem into a
    permanent one for the customer.
    """

    def __init__(self, detail: str):
        self.detail = detail
        super().__init__(detail)

_PROMPT = """You are writing a reusable extractor for one web page.

Given the HTML of a single page, return CSS selectors that locate each field.
Return ONLY a JSON object, no prose, with this exact shape:

{{
  "fields": {{
{field_lines}
  }},
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
- For a field marked as matching every element, give a selector that matches
  all of them; its mode is usually "attr:src".
- Search the WHOLE document before giving up on a field — on many sites the
  value sits far below the title, in a buy box, sticky bar, or configurator
  near the end of the markup.
- Use null for any field the page does not expose.
- A field asked for as a number must point at an element whose text holds
  digits. A meta description is tidy, sits at the top of the page, and reads
  well -- and it is not a price. Prefer the element a visitor would read the
  value from.
- Never read a value out of `class`, `id`, `style` or `role`. Those describe
  the markup, not the content.
- For a price, select the element holding the **current selling price** and
  nothing else. On a discounted page that is the sale price, not the one
  struck through or labelled as the normal price. Never select their common
  parent: its text comes out as "2,999,-1,899,-" and means nothing. The
  page you are reading may happen to have no discount today while others on
  the same site do, so prefer a selector that names the current price
  specifically -- `.sale-prijs`, `ins`, `.price--now` -- over one that would
  pick up both when a discount appears.
- For stock or availability, select the element that states it in words --
  "In stock", "Op voorraad", "2 left". The add-to-cart button is not a
  statement of availability; use it only when the page says nothing else,
  and never prefer it to real text.

HTML:
```
{html}
```"""


_ROWS_PROMPT = """You are writing a reusable extractor for one web page that
lists MANY items — a search result, a category, a table of records.

Find the repeating container that wraps exactly one item, then write each
field's selector as it appears INSIDE that container. Return ONLY a JSON
object, no prose, with this exact shape:

{{
  "item": "<css selector matching every item container, and nothing else>",
  "fields": {{
{field_lines}
  }},
  "modes": {{
     "<field>": "text" | "attr:<name>"
  }}
}}

Rules:
- "item" must match one element per item. Count them: a page showing twenty
  results should match twenty elements — not one wrapper around all of them,
  and not forty nested halves.
- Field selectors are relative to a container. Write ".price", not
  "article.card .price", and never repeat the container selector inside them.
- Prefer stable hooks (data- attributes, itemprop, semantic classes) over
  nth-child chains and over hashed build classes.
- For a value that lives in an attribute — a link's href, an image's src, a
  meta tag's content — set that field's mode to "attr:<name>".
- Use null for any field the item does not show.
- A field asked for as a number must point at an element whose text holds
  digits. A meta description is tidy, sits at the top of the page, and reads
  well -- and it is not a price. Prefer the element a visitor would read the
  value from.
- Never read a value out of `class`, `id`, `style` or `role`. Those describe
  the markup, not the content.
- For a price, select the element holding the **current selling price** and
  nothing else. On a discounted page that is the sale price, not the one
  struck through or labelled as the normal price. Never select their common
  parent: its text comes out as "2,999,-1,899,-" and means nothing. The
  page you are reading may happen to have no discount today while others on
  the same site do, so prefer a selector that names the current price
  specifically -- `.sale-prijs`, `ins`, `.price--now` -- over one that would
  pick up both when a discount appears.
- For stock or availability, select the element that states it in words --
  "In stock", "Op voorraad", "2 left". The add-to-cart button is not a
  statement of availability; use it only when the page says nothing else,
  and never prefer it to real text.

HTML:
```
{html}
```"""


# Tags that never hold a product's own data. ``nav`` was 110,000 characters
# of one van listing -- more than half of everything the model was being
# charged to read.
_NOISE_TAGS = ("script", "style", "svg", "noscript", "template", "iframe",
               "nav", "footer", "aside", "picture", "source", "link")

# Blocks named for what they are: the "you may also like" strip, the cookie
# banner, the breadcrumb. Dropping them is worth more than the characters.
# A related-products carousel is a dozen complete products with prices, laid
# out in the shop's own markup, and a model asked where the price lives can
# answer with one of theirs -- which then replays, correctly, on every page,
# returning a number that belongs to a different item.
_ASIDE = re.compile(
    r"related|recommend|upsell|cross[-_]?sell|also[-_](?:bought|viewed|like)"
    r"|similar[-_]product|breadcrumb|cookie|cmplz|newsletter|subscribe"
    r"|(?:^|[-_ ])(?:menu|megamenu)(?:[-_ ]|$)", re.IGNORECASE)

# Attributes that carry huge values and say nothing about where a field is.
# Dropped outright rather than truncated: ninety-eight truncated srcsets is
# still twelve thousand characters of nothing.
_DROP_ATTRS = ("srcset", "data-srcset", "sizes", "style", "title",
               "loading", "decoding", "width", "height", "target", "rel",
               "tabindex", "aria-describedby", "aria-labelledby")
# ``content`` is the value itself on a meta tag, so it is kept, just capped.
_BULKY_ATTRS = ("content",)
_ATTR_VALUE_CAP = 120
# An element with no text and no image cannot hold a field. Icon wrappers,
# spacers and empty grid cells are 64,000 characters of one page.
_KEEP_EMPTY = ("img", "input", "meta", "title")

# Whole-document cap. Generous on purpose: synthesis happens once per site, and
# a browser-rendered page can easily run past 90k characters — the field you
# need (the price, typically) is often past the halfway mark, so an aggressive
# cap silently produces a recipe with holes in it.
DEFAULT_HTML_CAP = 200_000


def reduce_html(html: str, cap: int = DEFAULT_HTML_CAP) -> str:
    """Strip noise so a product page reaches the model cheaply and intact.

    Removes scripts, styles, navigation and the blocks a shop names after
    what they are -- related products, cookie banners, breadcrumbs -- drops
    comments and empty wrappers, and strips or caps attributes that carry no
    structural signal. What is left is tags, ids, classes and the page's own
    words, which is all selector synthesis depends on.

    Measured on four live pages, this is 46% of what the old version sent.
    One van listing reduced to 827KB of markup around 24KB of text; its
    navigation alone was 110KB, and the page was being truncated at the cap,
    so the model could be paying to read a menu instead of reading the price.
    """
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(list(_NOISE_TAGS)):
        tag.decompose()
    for comment in soup.find_all(string=lambda s: isinstance(s, Comment)):
        comment.extract()
    for tag in soup.find_all(True):
        if tag.parent is None:
            continue                       # already gone with an ancestor
        mark = " ".join(tag.get("class") or []) + " " + (tag.get("id") or "")
        if mark.strip() and _ASIDE.search(mark):
            tag.decompose()
    for tag in soup.find_all(True):
        for attr in list(tag.attrs):
            value = tag.attrs[attr]
            if attr in _DROP_ATTRS:
                del tag.attrs[attr]
            elif isinstance(value, str) and (
                    attr in _BULKY_ATTRS or len(value) > _ATTR_VALUE_CAP):
                tag.attrs[attr] = value[:_ATTR_VALUE_CAP]
    for tag in reversed(soup.find_all(True)):   # innermost first
        if tag.name in _KEEP_EMPTY or tag.get_text(strip=True):
            continue
        if tag.find(_KEEP_EMPTY):
            continue
        tag.decompose()
    text = re.sub(r"\s+", " ", str(soup))
    return text[:cap]


def recipe_from_text(text: str, origin: str = "",
                     schema_name: str = "product") -> SelectorRecipe | None:
    """Parse a model's reply into a :class:`SelectorRecipe`. Pure — no network.

    Tolerates ``` fences, stray prose, and either reply shape: the nested
    ``{"fields": {...}, "modes": {...}}`` form or a flat map of field->selector.
    """
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None

    modes = data.get("modes") if isinstance(data.get("modes"), dict) else {}
    raw_fields = data.get("fields")
    if not isinstance(raw_fields, dict):
        raw_fields = {k: v for k, v in data.items() if k != "modes"}

    item = data.get("item")
    fields = {str(k): str(v) for k, v in raw_fields.items()
              if v and isinstance(v, str) and k != "item"}
    if not fields:
        return None
    return SelectorRecipe(fields=fields,
                          modes={str(k): str(v) for k, v in modes.items()},
                          item=str(item) if isinstance(item, str) else "",
                          schema_name=schema_name, origin=origin)


class LlmExtractor:
    """Wraps the Anthropic call that turns HTML into a recipe."""

    def __init__(self, model: str = DEFAULT_MODEL, api_key: str | None = None,
                 client=None, html_cap: int = DEFAULT_HTML_CAP,
                 escalate_to: str | None = ESCALATION_MODEL):
        self.model = model
        self.escalate_to = escalate_to or None
        self._client = client
        self._api_key = api_key
        self.html_cap = html_cap

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
        # Identity-linked keys refuse to work without naming a workspace, and
        # the error only appears on the first real call -- in production, on a
        # customer's request. Passing it as a default header costs nothing when
        # the variable is unset, which is the case for ordinary keys.
        workspace = os.environ.get("ANTHROPIC_WORKSPACE_ID")
        headers = {"anthropic-workspace-id": workspace} if workspace else None
        self._client = anthropic.Anthropic(api_key=self._api_key,
                                           default_headers=headers)
        return self._client

    def synthesize(self, html: str, url: str,
                   schema: Schema = PRODUCT_SCHEMA, *,
                   rows: bool = False) -> SelectorRecipe | None:
        """Ask Claude for a recipe covering this page. One call per site.

        ``rows`` asks for a listing recipe instead: a container selector plus
        fields read inside it, so the page yields one record per card.

        Two models, one answer. The cheap one writes the recipe; if that
        recipe cannot answer the very page it was written from, the stronger
        one is asked instead. The caller sees one synthesis and is billed for
        one, because they asked for one site to be compiled -- the second
        call is ours to pay for, and at a fiftieth of the price of making
        every compile the expensive kind, it is a good trade.
        """
        prompt = (_ROWS_PROMPT if rows else _PROMPT).format(
            field_lines=schema.prompt_lines(),
            html=reduce_html(html, cap=self.html_cap))
        recipe = self._ask(self.model, prompt, schema)
        if self.escalate_to and self.escalate_to != self.model                 and not self._answers(recipe, html, url, schema, rows):
            recipe = self._ask(self.escalate_to, prompt, schema) or recipe
        return recipe

    def _ask(self, model: str, prompt: str,
             schema: Schema) -> SelectorRecipe | None:
        client = self._get_client()
        try:
            msg = client.messages.create(
                model=model,
                max_tokens=1500,
                messages=[{"role": "user", "content": prompt}],
            )
        except Exception as e:
            # Everything the SDK raises from the call itself is the
            # provider's, named so the service can answer "try again"
            # instead of "your page is broken".
            raise ProviderError(f"{type(e).__name__}: {e}") from e
        text = "".join(b.text for b in msg.content
                       if getattr(b, "type", "") == "text")
        return recipe_from_text(text, origin=f"llm:{model}",
                                schema_name=schema.name)

    @staticmethod
    def _answers(recipe, html: str, url: str, schema: Schema,
                 rows: bool) -> bool:
        """Does this recipe read the page it was written from?

        The only check worth making, and the page itself makes it. A recipe
        that comes back empty here is either malformed or pointing at the
        wrong element, and both are worth the stronger model.
        """
        if recipe is None:
            return False
        from .selectors import SelectorExtractor      # avoids a cycle at import

        extractor = SelectorExtractor(recipe, schema)
        try:
            if rows:
                return bool(extractor.extract_rows(html, url))
            return schema.is_satisfied_by(extractor.extract_values(html, url))
        except Exception:
            return False
