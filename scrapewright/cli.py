"""Command line: `scrapewright detect|run|crawl|add|list|mcp`."""

from __future__ import annotations

import argparse
import json
import sys

from .cache import RecipeCache
from .detect import detect
from .export import write_any
from .models import Record
from .pipeline import Scrapewright
from .schema import PRODUCT_SCHEMA, Schema


def _emit(item) -> None:
    if isinstance(item, Record):
        payload = {**item.data, "url": item.url, "source_platform": item.source_platform}
    else:
        payload = item.model_dump(exclude={"raw"})
    print(json.dumps(payload, default=str, ensure_ascii=False))


def _schema_from(args) -> Schema:
    """`--field name[:kind]` (repeatable) declares a custom schema."""
    fields = getattr(args, "field", None)
    if not fields:
        return PRODUCT_SCHEMA
    return Schema.from_names(fields, name=getattr(args, "schema_name", None) or "custom")


def _deliver(items, out: str | None, label: str) -> None:
    """Either stream JSONL to stdout or write a .csv/.xlsx/.jsonl file."""
    if out:
        path = write_any(items, out)
        print(f"# {len(items)} items from {label} -> {path}", file=sys.stderr)
    else:
        for item in items:
            _emit(item)
        print(f"# {len(items)} items from {label}", file=sys.stderr)


def cmd_detect(args) -> int:
    det = detect(args.url)
    print(f"{det.base}\n  platform: {det.kind}\n  catalog:  {det.catalog_endpoint or '-'}\n  note:     {det.note}")
    return 0


def cmd_run(args) -> int:
    schema = _schema_from(args)
    custom = schema.name != PRODUCT_SCHEMA.name

    with Scrapewright(js=args.js) as sw:
        # A custom schema is always page-scoped: there is no platform API that
        # knows about the caller's fields.
        if args.page or custom:
            record = sw.extract(args.url, schema, allow_llm=not args.no_llm)
            if record is None:
                print("nothing extracted (try --js if the site renders client-side)",
                      file=sys.stderr)
                return 1
            _deliver([record], args.out, schema.name)
            return 0

        det = detect(args.url)
        if det.kind == "generic":
            # Single custom-HTML page — fall through to page mode automatically.
            product = sw.scrape_page(args.url, allow_llm=not args.no_llm)
            if product is None:
                print("no product extracted (try a direct product URL, or --js "
                      "if the site renders client-side)", file=sys.stderr)
                return 1
            _deliver([product], args.out, "page")
            return 0

        products = list(sw.scrape_catalog(args.url, max_items=args.max))
        _deliver(products, args.out, det.kind)
        return 0


def cmd_crawl(args) -> int:
    schema = _schema_from(args)
    with Scrapewright(js=args.js) as sw:
        items = list(sw.crawl_records(args.url, schema, max_items=args.max,
                                      allow_llm=not args.no_llm))
    _deliver(items, args.out, f"crawl:{schema.name}")
    if not items:
        print("nothing found — try --js if the site renders client-side",
              file=sys.stderr)
    return 0 if items else 1


def cmd_add(args) -> int:
    schema = _schema_from(args)
    with Scrapewright(js=args.js) as sw:
        record = sw.extract(args.url, schema, allow_llm=True)
    recipe = RecipeCache().get(args.url, schema.name)
    if recipe is None:
        print("no reusable recipe was cached (page may be JSON-LD or unparseable)",
              file=sys.stderr)
    else:
        print(f"cached recipe for {args.url} [{schema.name}]:", file=sys.stderr)
        print(json.dumps(recipe.model_dump(), indent=2, ensure_ascii=False), file=sys.stderr)
    if record is not None:
        _emit(record)
    return 0


def cmd_list(args) -> int:
    keys = RecipeCache().domains()
    if not keys:
        print("no cached recipes yet", file=sys.stderr)
    for k in keys:
        print(k)
    return 0


def cmd_mcp(args) -> int:
    """Serve the tools over MCP so an AI agent can call them."""
    from .mcp_server import build_server
    build_server().run(transport=args.transport)
    return 0


def _add_common(parser, *, listing: bool = False) -> None:
    parser.add_argument("--max", type=int, default=None,
                        help="Cap the number of items")
    parser.add_argument("--no-llm", action="store_true",
                        help="Never call the LLM; deterministic paths only")
    parser.add_argument("-o", "--out", default=None,
                        help="Write to a file: .csv, .xlsx, or .jsonl")
    parser.add_argument("--js", action="store_true",
                        help="Render pages in a headless browser when the static "
                             "fetch comes up empty (needs scrapewright[js])")
    parser.add_argument("-f", "--field", action="append", default=None,
                        metavar="NAME[:KIND]",
                        help="Declare a custom field instead of the product schema; "
                             "repeatable. KIND is text|number|url|list, e.g. "
                             "-f title -f salary:number -f tags:list")
    parser.add_argument("--schema-name", default=None,
                        help="Name for the custom schema (used as the cache key)")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="scrapewright",
                                description="Give it a URL, it writes the scraper.")
    sub = p.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("detect", help="Report the platform behind a URL")
    d.add_argument("url")
    d.set_defaults(func=cmd_detect)

    r = sub.add_parser("run", help="Scrape a catalog (Shopify/Woo) or a single page")
    r.add_argument("url")
    r.add_argument("--page", action="store_true", help="Force single-page mode")
    _add_common(r)
    r.set_defaults(func=cmd_run)

    c = sub.add_parser("crawl", help="Walk a whole site from one listing/category URL")
    c.add_argument("url")
    _add_common(c, listing=True)
    c.set_defaults(func=cmd_crawl)

    a = sub.add_parser("add", help="Synthesize and cache a recipe for one page")
    a.add_argument("url")
    _add_common(a)
    a.set_defaults(func=cmd_add)

    ls = sub.add_parser("list", help="List cached recipe keys")
    ls.set_defaults(func=cmd_list)

    m = sub.add_parser("mcp", help="Run as an MCP server for AI agents")
    m.add_argument("--transport", default="stdio",
                   choices=["stdio", "sse", "streamable-http"],
                   help="MCP transport (default: stdio)")
    m.set_defaults(func=cmd_mcp)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
