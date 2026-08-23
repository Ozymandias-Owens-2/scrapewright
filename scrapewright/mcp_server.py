"""Expose scrapewright to AI agents over the Model Context Protocol.

An agent that can call these tools gains one capability it otherwise lacks:
turning an arbitrary web page into structured data it can reason over — without
anyone hand-writing a parser for that site first, and without the agent burning
tokens re-reading raw HTML on every page.

The cost story matters here more than anywhere. A naive "agent reads the page"
loop pays model tokens per page forever. These tools pay once per *site*: the
first call compiles a selector recipe, every later call replays it for free.
An agent crawling 500 pages spends one synthesis, not five hundred.

Run it::

    pip install "scrapewright[mcp,llm]"
    scrapewright mcp

Then point an MCP client at that command. Extraction of custom sites needs
``ANTHROPIC_API_KEY`` in the server's environment; platform sites (Shopify,
WooCommerce) and pages with schema.org markup need no key at all.
"""

from __future__ import annotations

from typing import Any

from .cache import RecipeCache
from .detect import detect
from .export import write_any
from .models import Product, Record
from .pipeline import Scrapewright
from .schema import PRODUCT_SCHEMA, Schema

SERVER_NAME = "scrapewright"
SERVER_INSTRUCTIONS = (
    "Turn web pages into structured data. Call detect_site first when you do "
    "not know what a site runs on. Use scrape_catalog for Shopify/WooCommerce "
    "stores (free, no model tokens). Use extract_page for a single page and "
    "crawl_site to walk a listing. Pass `fields` to define your own schema "
    "(e.g. ['title', 'salary:number', 'tags:list']) when the page is not a "
    "product. Set js=true only if a page renders its content client-side."
)


def _product_payload(p: Product) -> dict[str, Any]:
    data = p.model_dump(exclude={"raw"})
    data["price"] = str(data["price"]) if data.get("price") is not None else None
    return data


def _record_payload(r: Record) -> dict[str, Any]:
    return {"url": r.url, "schema": r.schema_name,
            "source": r.source_platform,
            "data": {k: (str(v) if not isinstance(v, (list, str, bool, int, float)) else v)
                     for k, v in r.data.items()}}


def _schema_for(fields: list[str] | None) -> Schema:
    if not fields:
        return PRODUCT_SCHEMA
    return Schema.from_names(fields, name="custom")


def build_server():
    """Construct the MCP server. Imported lazily so the SDK stays optional."""
    try:
        from mcp.server import MCPServer
    except ImportError as e:  # pragma: no cover - env dependent
        raise RuntimeError(
            "The MCP server needs the 'mcp' package. "
            'Install it with:  pip install "scrapewright[mcp]"'
        ) from e

    from . import __version__

    server = MCPServer(name=SERVER_NAME, version=__version__,
                       instructions=SERVER_INSTRUCTIONS)

    @server.tool()
    def detect_site(url: str) -> dict[str, Any]:
        """Report what platform a site runs on and whether it exposes a free
        catalog API. Cheap and always worth calling before a large job."""
        det = detect(url)
        return {"base": det.base, "platform": det.kind,
                "catalog_endpoint": det.catalog_endpoint, "note": det.note,
                "free": det.kind in ("shopify", "woocommerce")}

    @server.tool()
    def scrape_catalog(url: str, max_items: int = 50) -> dict[str, Any]:
        """Pull a whole product catalog from a Shopify or WooCommerce store.

        Fully deterministic — no model tokens are spent. Fails if the site has
        no known catalog API; use crawl_site for those.
        """
        with Scrapewright() as sw:
            try:
                products = list(sw.scrape_catalog(url, max_items=max_items))
            except ValueError as e:
                return {"error": str(e), "products": []}
        return {"count": len(products),
                "products": [_product_payload(p) for p in products]}

    @server.tool()
    def extract_page(url: str, fields: list[str] | None = None,
                     js: bool = False) -> dict[str, Any]:
        """Extract structured data from ONE page.

        ``fields`` declares your own schema — ``["title", "salary:number",
        "tags:list"]``; omit it for the built-in product schema. The first call
        against a new site compiles a reusable recipe (one model call); later
        calls on that site replay it for free. Set ``js=true`` only when the
        page renders client-side.
        """
        schema = _schema_for(fields)
        with Scrapewright(js=js) as sw:
            record = sw.extract(url, schema)
        if record is None:
            return {"error": "nothing extracted; try js=true if the page renders "
                             "client-side, or check the URL is a content page",
                    "url": url}
        payload = _record_payload(record)
        payload["complete"] = schema.is_satisfied_by(record.data)
        return payload

    @server.tool()
    def crawl_site(listing_url: str, fields: list[str] | None = None,
                   max_items: int = 25, js: bool = False,
                   save_to: str | None = None) -> dict[str, Any]:
        """Walk a site from one listing/category URL and extract every item.

        Shopify/WooCommerce stores short-circuit to their free catalog API.
        For custom sites the first page compiles a recipe and the rest replay
        it. ``save_to`` optionally writes a .csv/.xlsx/.jsonl file and returns
        the path instead of a large payload.
        """
        schema = _schema_for(fields)
        with Scrapewright(js=js) as sw:
            records = list(sw.crawl_records(listing_url, schema, max_items=max_items))
        result: dict[str, Any] = {"count": len(records)}
        if save_to:
            result["saved_to"] = str(write_any(records, save_to))
        else:
            result["records"] = [_record_payload(r) for r in records]
        if not records:
            result["hint"] = ("no items found — try js=true, or pass a URL that "
                              "lists items rather than the homepage")
        return result

    @server.tool()
    def list_learned_sites() -> dict[str, Any]:
        """List sites already compiled into cached recipes. These cost nothing
        to scrape again."""
        return {"sites": RecipeCache().domains()}

    return server


def main() -> int:
    build_server().run(transport="stdio")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
