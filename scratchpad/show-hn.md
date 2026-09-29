# Show HN — 30 September 2026, 15:00 Amsterdam

## Title (paste into "title")

Show HN: Scrapewright – a scraper an LLM writes once, then replays for free

## URL (paste into "url")

https://scrapewright.app

## First comment (post immediately after submitting)

Hi HN. I'm an economics student in Spain. I built this because every scraping
tool I tried charged per page, forever, for work that only needs doing once.

A site's structure changes rarely, so paying a model to read every page is
paying again and again for the same answer. Scrapewright calls a model once per
site to produce a set of CSS selectors, caches that recipe, and replays it
deterministically on every page after. No model in the loop, nothing more to pay.
Shopify and WooCommerce stores skip the model entirely — they publish catalogue
JSON, so those are deterministic and free from the first request.

There's a demo on the front page that needs no signup. `pip install scrapewright`
gives you the CLI and the library under MIT. Agents can connect over MCP, either
locally or by URL at https://scrapewright.app/mcp with an API key.

Four things I got wrong first, which may be worth more than the tool:

- Billing correctness is harder than scraping. My first version counted the model
  call before it happened and charged in a `finally` block — two failed extracts
  billed a customer 600 credits. Now a request that errors is never charged, and
  synthesis is counted after the call returns.
- Cache reads have to happen immediately before the expensive path, not at the
  top of the function. A JS-rendered page compiled twice in one job because the
  browser pass never re-read the cache the static pass had just filled.
- robots.txt has a spec (RFC 9309) with non-obvious rules: 404 means allow,
  401/403 means deny everything, 5xx means allow. It sends an honest User-Agent
  and obeys robots by default.
- SSRF is the real risk in a service that fetches URLs for strangers. Checking
  the hostname is not enough: every resolved address has to be public, redirects
  have to be re-checked at each hop, and 0177.0.0.1 is a valid spelling of
  localhost. Mine was exploitable until I tested it against my own dev server.

Known limits, so nobody is surprised: recipes break when a site redesigns
(booking.com's build-hashed class names change every deploy), there is a
DNS-rebinding window I have not closed, and the recipe cache is shared between
customers by design — that is exactly what makes an already-compiled site free
for the next person.

Source: https://github.com/Ozymandias-Owens-2/scrapewright

Happy to answer anything.
