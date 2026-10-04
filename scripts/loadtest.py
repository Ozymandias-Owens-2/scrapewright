"""Hit the service the way a launch day would, and report what broke.

Two different loads, because a launch is two different things:

* **the site** -- everyone who clicks the link. No key, no scraping, just
  the pages and /health. This is the part a front page actually sends, and
  the part that must not fall over.
* **the API** -- the few who try it. Needs a key, costs credits, and is
  deliberately slow: one render at a time by design.

Run the site load first and alone; it needs nothing and risks nothing::

    python scripts/loadtest.py site

Then, with a key you do not mind spending credits from::

    SCRAPEWRIGHT_KEY=sw_... python scripts/loadtest.py api

What to look for is in the summary: any non-2xx that is not a 429, and the
p95. A 429 under load is the service working -- it is the per-key limit
saying no, which is what it is for.
"""

from __future__ import annotations

import os
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor

import requests

BASE = os.environ.get("SCRAPEWRIGHT_BASE", "https://scrapewright.app")
KEY = os.environ.get("SCRAPEWRIGHT_KEY", "")

# Pages a visitor from an orange link actually loads.
SITE_PATHS = ["/", "/health", "/docs", "/account", "/terms", "/privacy"]

# Real pages, static and rendered, that the API already holds recipes for --
# so this measures the service under load rather than buying syntheses.
API_URLS = [
    ("https://books.toscrape.com/catalogue/a-light-in-the-attic_1000/index.html", False),
    ("https://books.toscrape.com/catalogue/tipping-the-velvet_999/index.html", False),
    ("https://quotes.toscrape.com/js/", True),
]


def _session(concurrency: int) -> requests.Session:
    """A session whose connection pool is as wide as the load.

    The default pool holds ten connections, so a hundred threads on one
    session queue behind ten and the run measures the client. The first
    attempt at this reported every request failing at 150 concurrent while
    the service answered a single request in 66 milliseconds throughout.
    """
    session = requests.Session()
    adapter = requests.adapters.HTTPAdapter(pool_connections=concurrency,
                                            pool_maxsize=concurrency)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


def _timed(call):
    start = time.perf_counter()
    try:
        status = call()
    except Exception as e:                      # a timeout is a result too
        return None, f"{type(e).__name__}", time.perf_counter() - start
    return status, "", time.perf_counter() - start


def _report(name: str, results: list[tuple]) -> int:
    took = sorted(r[2] for r in results)
    codes: dict[str, int] = {}
    for status, error, _ in results:
        label = error or str(status)
        codes[label] = codes.get(label, 0) + 1

    def pct(p):
        return took[min(len(took) - 1, int(len(took) * p))]

    print(f"\n{name}: {len(results)} requests")
    for label, count in sorted(codes.items()):
        print(f"   {label:>22}  {count}")
    print(f"   {'median':>22}  {statistics.median(took):.2f}s")
    print(f"   {'p95':>22}  {pct(0.95):.2f}s")
    print(f"   {'slowest':>22}  {took[-1]:.2f}s")

    bad = sum(c for label, c in codes.items()
              if label in ("", "None") or not label.startswith(("2", "4"))
              or label == "500" or label == "502")
    return bad


def site(concurrency: int = 40, rounds: int = 10) -> int:
    """Everyone who clicks the link, at once. No key, nothing charged."""
    session = _session(concurrency)
    jobs = [SITE_PATHS[i % len(SITE_PATHS)]
            for i in range(concurrency * rounds)]

    def one(path):
        return _timed(lambda: session.get(BASE + path, timeout=30).status_code)

    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        results = list(pool.map(one, jobs))
    return _report(f"site, {concurrency} at a time", results)


def api(concurrency: int = 6, rounds: int = 4) -> int:
    """The few who try it for real. Costs credits; needs a key."""
    if not KEY:
        sys.exit("set SCRAPEWRIGHT_KEY first -- this one spends credits")
    session = _session(concurrency)
    session.headers.update({"X-API-Key": KEY})
    jobs = [API_URLS[i % len(API_URLS)] for i in range(concurrency * rounds)]

    def one(job):
        url, js = job
        return _timed(lambda: session.post(
            f"{BASE}/v1/extract", json={"url": url, "js": js},
            timeout=180).status_code)

    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        results = list(pool.map(one, jobs))
    bad = _report(f"api, {concurrency} at a time", results)

    health = session.get(f"{BASE}/health", timeout=30).json()
    print(f"\n   renderer: {health.get('renderer')!r}, "
          f"local fallbacks since boot: {health.get('renderer_fallbacks')}")
    if health.get("renderer_fallbacks"):
        print("   ^ the API rendered in its own process. That is the thing "
              "the separate machine exists to stop.")
        bad += 1
    return bad


if __name__ == "__main__":
    which = sys.argv[1] if len(sys.argv) > 1 else "site"
    failed = {"site": site, "api": api}[which]()
    print("\nverdict:", "clean" if not failed else f"{failed} bad responses")
    sys.exit(1 if failed else 0)
