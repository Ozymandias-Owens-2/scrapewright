"""Command line: `scrapewright detect|run|add|list`."""

from __future__ import annotations

import argparse
import json
import sys

from .cache import RecipeCache
from .detect import detect
from .export import write_products
from .pipeline import Scrapewright


def _emit(product) -> None:
    print(json.dumps(product.model_dump(exclude={"raw"}), default=str, ensure_ascii=False))


def cmd_detect(args) -> int:
    det = detect(args.url)
    print(f"{det.base}\n  platform: {det.kind}\n  catalog:  {det.catalog_endpoint or '—'}\n  note:     {det.note}")
    return 0


def _deliver(products, out: str | None, label: str) -> None:
    """Either stream JSONL to stdout or write a .csv/.xlsx/.jsonl file."""
    if out:
        path = write_products(products, out)
        print(f"# {len(products)} products from {label} -> {path}", file=sys.stderr)
    else:
        for p in products:
            _emit(p)
        print(f"# {len(products)} products from {label}", file=sys.stderr)


def cmd_run(args) -> int:
    with Scrapewright(js=args.js) as sw:
        if args.page:
            product = sw.scrape_page(args.url, allow_llm=not args.no_llm)
            if product is None:
                print("no product extracted", file=sys.stderr)
                return 1
            _deliver([product], args.out, "page")
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
    with Scrapewright(js=args.js) as sw:
        products = list(sw.crawl(args.url, max_items=args.max, allow_llm=not args.no_llm))
    _deliver(products, args.out, "crawl")
    if not products:
        print("nothing found — try --js if the site renders client-side",
              file=sys.stderr)
    return 0 if products else 1


def cmd_add(args) -> int:
    with Scrapewright(js=args.js) as sw:
        product = sw.scrape_page(args.url, allow_llm=True)
    recipe = RecipeCache().get(args.url)
    if recipe is None:
        print("no reusable recipe was cached (page may be JSON-LD or unparseable)",
              file=sys.stderr)
    else:
        print(f"cached recipe for {args.url}:", file=sys.stderr)
        print(json.dumps(recipe.model_dump(), indent=2, ensure_ascii=False), file=sys.stderr)
    if product is not None:
        _emit(product)
    return 0


def cmd_list(args) -> int:
    domains = RecipeCache().domains()
    if not domains:
        print("no cached recipes yet", file=sys.stderr)
    for d in domains:
        print(d)
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="scrapewright", description="Give it a store URL, it writes the scraper.")
    sub = p.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("detect", help="Report the platform behind a URL")
    d.add_argument("url")
    d.set_defaults(func=cmd_detect)

    r = sub.add_parser("run", help="Scrape a catalog (Shopify/Woo) or a single product page")
    r.add_argument("url")
    r.add_argument("--max", type=int, default=None, help="Cap catalog items")
    r.add_argument("--page", action="store_true", help="Force single-page mode")
    r.add_argument("--no-llm", action="store_true", help="Never call the LLM; deterministic paths only")
    r.add_argument("-o", "--out", default=None, help="Write to a file: .csv, .xlsx, or .jsonl")
    r.add_argument("--js", action="store_true", help="Render pages in a headless browser when the static fetch comes up empty (needs scrapewright[js])")
    r.set_defaults(func=cmd_run)

    c = sub.add_parser("crawl", help="Walk a whole store from one listing/category URL")
    c.add_argument("url")
    c.add_argument("--max", type=int, default=None, help="Cap total products")
    c.add_argument("--no-llm", action="store_true", help="Never call the LLM; deterministic paths only")
    c.add_argument("-o", "--out", default=None, help="Write to a file: .csv, .xlsx, or .jsonl")
    c.add_argument("--js", action="store_true", help="Render pages in a headless browser when the static fetch comes up empty (needs scrapewright[js])")
    c.set_defaults(func=cmd_crawl)

    a = sub.add_parser("add", help="Synthesize and cache a recipe for a custom-HTML product page")
    a.add_argument("url")
    a.add_argument("--js", action="store_true", help="Render the page in a headless browser (needs scrapewright[js])")
    a.set_defaults(func=cmd_add)

    ls = sub.add_parser("list", help="List cached recipe domains")
    ls.set_defaults(func=cmd_list)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
