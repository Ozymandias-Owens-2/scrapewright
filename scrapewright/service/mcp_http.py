"""The same MCP tools as ``scrapewright mcp``, served over HTTP from the paid
service instead of a process on the agent's machine.

An agent adds one line of config -- the URL and its ``X-API-Key`` -- and pays
in the same credits as the REST API, because each tool here *is* the REST
endpoint: same key check, same quota gate, same charge. There is deliberately
no second billing path to drift out of step.

Tools run in a worker thread (the SDK does that for sync functions), which is
what the Playwright sync API needs.
"""

# No `from __future__ import annotations`: the SDK evaluates tool signatures
# at registration, and the lazily imported Context must resolve then.
import time
from typing import Any, Callable

from fastapi import FastAPI, HTTPException

from .. import __version__
from ..mcp_server import SERVER_INSTRUCTIONS

# How long crawl_site holds the connection before handing back a job id. Long
# enough for most listings; short enough that no proxy in between gives up.
CRAWL_WAIT_SECONDS = 240


def mount_hosted_mcp(app: FastAPI, *, require_key: Callable[[str], Any],
                     detect: Callable, extract: Callable, crawl: Callable,
                     job: Callable, usage: Callable) -> None:
    """Attach ``/mcp`` to the FastAPI app. The callables are the REST handlers
    themselves, called with a resolved key in place of the ``Depends``."""
    try:
        from mcp.server import MCPServer
        from mcp.server.mcpserver import Context
    except ImportError:  # pragma: no cover - env dependent
        return

    from .app import CrawlRequest, DetectRequest, ExtractRequest

    server = MCPServer(name="scrapewright", version=__version__,
                       instructions=SERVER_INSTRUCTIONS
                       + " Every call needs the X-API-Key header; get a key at "
                         "https://scrapewright.app.",
                       website_url="https://scrapewright.app")

    def key_for(ctx: Context):
        # The REST gate itself: resolves, counts the request, raises 401.
        return require_key(ctx.headers.get("x-api-key", ""))

    def guarded(fn: Callable[[], dict[str, Any]]) -> dict[str, Any]:
        """REST raises; a tool returns. Same message, different envelope."""
        try:
            return fn()
        except HTTPException as e:
            return {"error": e.detail, "status": e.status_code}

    @server.tool()
    def detect_site(url: str, ctx: Context) -> dict[str, Any]:
        """Report what platform a site runs on and which strategy to use.
        Cheap; call it before a large job."""
        return guarded(lambda: detect(DetectRequest(url=url), key_for(ctx)))

    @server.tool()
    def extract_page(url: str, ctx: Context, fields: list[str] | None = None,
                     js: bool = False) -> dict[str, Any]:
        """Extract structured data from ONE page. ``fields`` declares your own
        schema, e.g. ["title", "salary:number", "tags:list"]; omit it for the
        product schema. First call on a new site compiles a recipe (300
        credits); later calls replay it for 1 credit per row."""
        return guarded(lambda: extract(
            ExtractRequest(url=url, fields=fields, js=js), key_for(ctx)))

    @server.tool()
    def crawl_site(listing_url: str, ctx: Context, fields: list[str] | None = None,
                   max_items: int = 25, js: bool = False,
                   scroll: int = 0) -> dict[str, Any]:
        """Walk a site from one listing URL and extract every item. Waits up
        to four minutes; a longer crawl returns a job_id to pass to
        crawl_status."""
        def run() -> dict[str, Any]:
            key = key_for(ctx)
            started = crawl(CrawlRequest(url=listing_url, fields=fields, js=js,
                                         max_items=max_items, scroll=scroll), key)
            deadline = time.monotonic() + CRAWL_WAIT_SECONDS
            while time.monotonic() < deadline:
                state = job(started["job_id"], key)
                if state["status"] in ("done", "error"):
                    return state
                time.sleep(2)
            return {**started, "hint": "still running; call crawl_status with this job_id"}
        return guarded(run)

    @server.tool()
    def crawl_status(job_id: str, ctx: Context) -> dict[str, Any]:
        """Fetch a crawl that outlived its call."""
        return guarded(lambda: job(job_id, key_for(ctx)))

    @server.tool()
    def account(ctx: Context) -> dict[str, Any]:
        """Credits left and this month's usage for the key in use."""
        return guarded(lambda: usage(key_for(ctx)))

    # Stateless + JSON: no session to lose on a redeploy, and an answer any
    # HTTP client can read without an SSE parser. The route is lifted out of
    # the SDK's Starlette app rather than mounted, so `/mcp` answers directly
    # instead of redirecting to `/mcp/`; its lifespan is run by ours.
    mcp_app = server.streamable_http_app(streamable_http_path="/mcp",
                                         stateless_http=True, json_response=True,
                                         host="scrapewright.app")
    app.router.routes.extend(mcp_app.routes)
    app.state.mcp_session_manager = server.session_manager
