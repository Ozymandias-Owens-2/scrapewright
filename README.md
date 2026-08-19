# scrapewright

[![PyPI](https://img.shields.io/pypi/v/scrapewright)](https://pypi.org/project/scrapewright/)
[![Python](https://img.shields.io/pypi/pyversions/scrapewright)](https://pypi.org/project/scrapewright/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

**Give it a store URL. It writes the scraper.**

Most e-commerce catalog scraping splits into two worlds: sites on a known
platform (Shopify, WooCommerce) that expose a clean JSON feed, and everything
else — bespoke HTML where you hand-write a parser per site and re-write it every
time the markup shifts. scrapewright collapses both into one call:

1. **Detect** the platform behind a URL.
2. For known platforms, **extract deterministically** from their public catalog
   API — free, stable, no LLM.
3. For custom HTML, **synthesize a reusable extractor once** with an LLM, cache
   it, and **replay it deterministically forever after**.

The LLM is a *compiler*, not a runtime. It runs **once per site** to produce a
recipe of CSS selectors; every page after that is parsed by plain BeautifulSoup
at zero marginal cost. That is the whole cost-control story — no per-page model
calls, no token bill that scales with your crawl.

```
                    ┌─────────────┐
   store URL  ───▶  │   detect    │
                    └──────┬──────┘
        ┌──────────────────┼──────────────────┐
        ▼                  ▼                   ▼
    shopify            woocommerce         generic HTML
   products.json      wc/store/products    (page mode)
        │                  │                   │
        │  deterministic   │                   ▼
        │  (free)          │            cached recipe? ──yes──▶ replay (free)
        └────────┬─────────┘                   │ no
                 ▼                              ▼
             Product{}  ◀───── selectors ── JSON-LD? ──yes──▶ Product{} (free)
                 ▲                              │ no
                 │                              ▼
                 └──────── replay ◀── LLM synthesizes recipe ONCE ──▶ cache
```

Everything normalizes to one `Product` shape, so downstream code never knows or
cares which path a record came from.

## Install

```bash
pip install scrapewright          # deterministic paths (Shopify, Woo, JSON-LD)
pip install "scrapewright[llm]"   # + LLM recipe synthesis for custom HTML
```

## Use it

```python
from scrapewright import Scrapewright

sw = Scrapewright()

# Catalog mode — a whole Shopify/WooCommerce store, deterministically
for product in sw.scrape_catalog("https://shop.example.com", max_items=200):
    print(product.brand, product.title, product.price, product.currency)

# Page mode — one custom-HTML product page.
# First call: tries JSON-LD (free); if absent, the LLM writes a recipe once.
# Every later call on that domain: replayed from the cached recipe, no LLM.
item = sw.scrape_page("https://boutique.example.com/products/wool-coat")
print(item.model_dump(exclude={"raw"}))

# Crawl mode — walk a WHOLE custom store from one listing/category URL.
# The frontier discovers product pages (deterministic, free); the first page
# pays the single synthesis cost, every other page replays the recipe.
for product in sw.crawl("https://boutique.example.com/collection", max_items=100):
    print(product.title, product.price)
```

### CLI

```bash
scrapewright detect https://shop.example.com          # what platform is this?
scrapewright run    https://shop.example.com --max 50 # scrape a catalog → JSONL
scrapewright crawl  https://boutique.example.com/collection -o products.xlsx
scrapewright run    https://shop.example.com -o products.csv   # Excel-ready CSV
scrapewright add    https://boutique.example.com/products/coat  # learn a site
scrapewright run    https://boutique.example.com/products/coat --no-llm
scrapewright list                                     # cached recipe domains
```

`-o` writes `.csv` (Excel-ready, UTF-8 BOM), `.xlsx` (`pip install scrapewright[excel]`),
or `.jsonl`; without it, products stream to stdout as JSONL.

## The `Product` shape

```python
url: str            # canonical product URL
title: str
brand: str | None
price: Decimal | None   # parsed from "1,250.00" / "1.250,00" / "€1290" alike
currency: str | None
available: bool | None
images: list[str]       # absolute URLs
sizes: list[str]
description: str | None
sku: str | None
source_platform: str    # shopify | woocommerce | json-ld | selector
```

A record is **usable** when it carries a title, a price, and a URL. The
validator (`scrapewright.coverage`) reports the usable ratio across a batch —
the number a recipe is trusted on before it's cached.

## How the pieces fit

| Module | Role |
|---|---|
| `detect` | Platform probe: Shopify → WooCommerce → generic |
| `extract/shopify`, `extract/woocommerce` | Deterministic catalog extractors |
| `extract/jsonld` | schema.org/Product from `<script type="application/ld+json">` — free, ~common |
| `extract/llm` | Synthesizes a `SelectorRecipe` from HTML — the one-time compile step |
| `extract/selectors` | Replays a recipe with BeautifulSoup — the deterministic runtime |
| `crawl` | Frontier: turns one listing URL into product URLs (pattern match + card-template fallback + pagination) — deterministic, no LLM |
| `cache` | Persists recipes keyed by domain, so the compile happens once |
| `validate` | Field-coverage scoring |
| `export` | Batch → `.csv` / `.xlsx` / `.jsonl` |
| `pipeline` | Orchestrates detect → extract → validate → cache → heal |

## Design notes

- **Deterministic paths run first.** Shopify JSON, the WooCommerce Store API, and
  JSON-LD cover a large share of real stores for free. The LLM is only ever
  reached for genuinely custom HTML.
- **Self-healing.** When a cached recipe stops producing usable products — the
  site changed its DOM — the page falls through to the free JSON-LD path and,
  failing that, a fresh synthesis replaces the stale recipe. A broken site heals
  on the next run instead of silently returning empty fields.
- **Bounded model spend.** Batch and crawl runs cap LLM calls at
  `max_synth_per_run` (default 3) — a site that resists synthesis cannot burn
  one model call per page. The bill is bounded no matter how large the crawl.
- **Provider-configurable.** The LLM extractor takes a `model` and works with any
  injected client; the default targets Anthropic's Claude via the official SDK.

## Testing

The deterministic paths are fully covered by offline fixtures — no network, no
model calls — so CI is green without an API key:

```bash
pip install "scrapewright[dev]"
pytest
```

## Status

v0.2 (alpha). Implemented and tested: catalog extraction (Shopify, WooCommerce),
page extraction (JSON-LD, LLM-synthesized selectors), recipe caching,
**self-healing re-synthesis** with a bounded per-run model budget, a
**crawl frontier** (one listing URL → the whole store), coverage validation, and
CSV / XLSX / JSONL export.

Roadmap: schema-agnostic extraction (bring your own field schema — the same
compile-once/replay-free loop for any structured site, not just product pages),
BigCommerce / Salesforce Commerce detectors, and JS-rendered-page support via an
optional Playwright fetcher.

## License

MIT — see [LICENSE](LICENSE).
