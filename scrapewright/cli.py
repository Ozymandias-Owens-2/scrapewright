"""Command line: `scrapewright detect|run|crawl|add|list|mcp`."""

from __future__ import annotations

import argparse
import os
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
    also = [m for m in det.matched if m != det.kind]
    lines = [
        det.base,
        f"  platform: {det.kind}" + (f" (also: {', '.join(also)})" if also else ""),
        f"  catalog:  {det.catalog_endpoint or '-'}",
        f"  strategy: {det.strategy}",
        f"  note:     {det.note}",
    ]
    if det.likely_needs_js:
        lines.append("  hint:     this platform renders client-side; add --js")
    print("\n".join(lines))
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


def cmd_serve(args) -> int:
    """Run the HTTP service."""
    try:
        import uvicorn
    except ImportError:
        print("The service needs FastAPI and uvicorn. Install with:", file=sys.stderr)
        print('    pip install "scrapewright[service]"', file=sys.stderr)
        return 1
    from .service.app import create_app
    from .service.store import Store

    app = create_app(store=Store(args.db))
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")
    return 0


def cmd_keys(args) -> int:
    """Mint, list and revoke API keys for the service."""
    from .service.store import Store

    store = Store(args.db)
    if args.action == "create":
        raw, record = store.create_key(label=args.label or "", plan=args.plan)
        print(f"key id:  {record.id}")
        print(f"plan:    {record.plan}")
        print(f"API key: {raw}")
        print("", file=sys.stderr)
        print("Store it now — only its hash is kept, so it cannot be shown again.",
              file=sys.stderr)
    elif args.action == "list":
        rows = store.list_keys()
        if not rows:
            print("no keys yet", file=sys.stderr)
        for k in rows:
            state = "active" if k.active else "revoked"
            print(f"{k.id}  {k.plan:<10} {state:<8} {k.created_at}  {k.label}")
    elif args.action == "revoke":
        print("revoked" if store.revoke(args.key_id) else "no such active key")
    return 0


def cmd_plans(args) -> int:
    """Show the credit model against the cost it has to cover."""
    from .service.credits import FREE_MONTHLY_CREDITS, PACKS, describe_costs
    from .service.pricing import free_allowance_worst_case, pack_margin

    costs = describe_costs()
    print("What a job costs, in credits:")
    print(f"  1 record delivered   {costs['record']:>4} credit")
    print(f"  1 browser render     {costs['render']:>4} credits")
    print(f"  1 new site compiled  {costs['new_site']:>4} credits")
    print("  page fetches         free (covered by the record they produce)")
    print("  detect / routing     free")
    print()

    head = f"{'pack':<10}{'credits':>10}{'price':>8}{'$/credit':>11}{'margin':>9}"
    print(head)
    print("-" * len(head))
    for pack in PACKS:
        m = pack_margin(pack)
        print(f"{pack.name:<10}{pack.credits:>10,}{'$' + str(pack.price_usd):>8}"
              f"{m['usd_per_credit']:>11.5f}{str(m['margin_pct']) + '%':>9}")
    print()
    print(f"Free: {FREE_MONTHLY_CREDITS:,} credits a month, resetting. Worst case "
          f"that costs us ${free_allowance_worst_case():.2f} per account.")
    print("Margin is measured on compiling a new site -- the only step that "
          "costs real money.")
    return 0


def cmd_credits(args) -> int:
    """Grant credits or read a balance."""
    from .service.credits import FREE_MONTHLY_CREDITS, PACKS_BY_NAME
    from .service.pricing import value_of
    from .service.store import Store

    store = Store(args.db)
    if args.action == "grant":
        amount = args.amount
        reason = args.reason or "manual grant"
        if args.pack:
            pack = PACKS_BY_NAME.get(args.pack)
            if pack is None:
                print(f"unknown pack {args.pack!r}; try: "
                      f"{', '.join(PACKS_BY_NAME)}", file=sys.stderr)
                return 1
            amount, reason = pack.credits, f"pack: {pack.name} (${pack.price_usd})"
        if not amount:
            print("give --amount N or --pack NAME", file=sys.stderr)
            return 1
        applied = store.grant(args.key_id, amount, reason,
                              idempotency_key=args.idempotency)
        if applied:
            print(f"granted {amount:,} credits to {args.key_id} ({reason})")
        else:
            # The idempotency key was already used -- almost always a replayed
            # payment webhook. Say so instead of claiming a grant that did not
            # happen; the balance below is the truth either way.
            print(f"already applied: {args.idempotency!r} was granted before, "
                  f"nothing added")
        print(f"balance now {store.balance(args.key_id):,}")
    elif args.action == "balance":
        balance = store.balance(args.key_id)
        print(f"{args.key_id}: {balance:,} credits (~${value_of(balance):.2f})")
        print(f"free allowance: {FREE_MONTHLY_CREDITS:,} credits/month")
        for entry in store.ledger(args.key_id, limit=args.limit):
            sign = "+" if entry["delta"] > 0 else ""
            print(f"  {entry['created_at']}  {sign}{entry['delta']:>8,}  "
                  f"{entry['reason']}")
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

    sv = sub.add_parser("serve", help="Run the HTTP service (API keys, quotas, jobs)")
    sv.add_argument("--host", default="0.0.0.0")
    sv.add_argument("--port", type=int, default=8000)
    sv.add_argument("--db", default=_default_db())
    sv.set_defaults(func=cmd_serve)

    k = sub.add_parser("keys", help="Manage service API keys")
    k.add_argument("action", choices=["create", "list", "revoke"])
    k.add_argument("key_id", nargs="?", default=None, help="for: revoke")
    k.add_argument("--label", default=None)
    k.add_argument("--plan", default="metered",
                   choices=["metered", "unlimited"],
                   help="metered keys spend credits; unlimited is for self-hosting")
    k.add_argument("--db", default=_default_db())
    k.set_defaults(func=cmd_keys)

    pl = sub.add_parser("plans", help="Show the credit model and its margins")
    pl.set_defaults(func=cmd_plans)

    cr = sub.add_parser("credits", help="Grant credits or read a balance")
    cr.add_argument("action", choices=["grant", "balance"])
    cr.add_argument("key_id")
    cr.add_argument("--amount", type=int, default=0, help="credits to grant")
    cr.add_argument("--pack", default=None,
                    help="grant a whole pack: starter | growth | scale")
    cr.add_argument("--reason", default=None)
    cr.add_argument("--idempotency", default=None,
                    help="payment id, so a replayed webhook cannot double-credit")
    cr.add_argument("--limit", type=int, default=10, help="ledger lines to show")
    cr.add_argument("--db", default=_default_db())
    cr.set_defaults(func=cmd_credits)

    m = sub.add_parser("mcp", help="Run as an MCP server for AI agents")
    m.add_argument("--transport", default="stdio",
                   choices=["stdio", "sse", "streamable-http"],
                   help="MCP transport (default: stdio)")
    m.set_defaults(func=cmd_mcp)
    return p


def _default_db() -> str:
    """Where the service keeps its data.

    Read from the environment so a container can point it at a mounted volume.
    Without this the Dockerfile's SCRAPEWRIGHT_DB was set and then ignored, and
    the credit ledger -- customers' paid balances -- would have been written
    inside the container and destroyed on every deploy.
    """
    return os.environ.get("SCRAPEWRIGHT_DB", "scrapewright_service.db")


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
