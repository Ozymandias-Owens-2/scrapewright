"""A browser on a machine that holds nothing worth stealing.

The API fetches pages chosen by strangers and renders some of them. That is
the job, and it means Chromium is the part most likely to be broken into:
the delivery mechanism for a renderer exploit is a URL, and we accept URLs
for money.

Until now the browser ran beside the API, as the same user, on the same
disk. An escape from Chromium's sandbox would land on the Stripe key, the
model key and the credentials to the backup bucket -- in the API's
environment, readable through /proc at the same uid -- plus write access to
the database and to the replica itself. Owning the browser would have meant
owning the business.

This service is the same browser with nothing around it. No secrets, no
volume, no database, no public address: it answers only on Fly's private
network, only to a caller holding a shared token, and it stops between
bursts so a compromise does not outlive the traffic that caused it. An
escape here lands on a container that can fetch web pages -- which is what
the product openly sells.

The checks come with it. This runs the same `BrowserFetcher`, so robots.txt
and the SSRF rules apply exactly as they do in-process; moving the browser
must not move it outside the rules.
"""

from __future__ import annotations

import os
import secrets

from fastapi import FastAPI, Header, HTTPException, status
from pydantic import BaseModel, Field

from .. import __version__
from .browser_pool import BrowserPool, NoBrowserSlot

RENDER_TOKEN_ENV = "RENDER_TOKEN"


class RenderRequest(BaseModel):
    url: str
    scroll: int = Field(0, ge=0, le=50)


def create_render_app(pool: BrowserPool | None = None) -> FastAPI:
    pool = pool or BrowserPool()
    expected = os.environ.get(RENDER_TOKEN_ENV, "")

    app = FastAPI(title="scrapewright renderer", version=__version__,
                  description="Renders a page and returns its HTML. "
                              "Private to the API that calls it.")
    app.state.browsers = pool

    def authorise(token: str) -> None:
        """Private networking is not authentication.

        Anything else in the same organisation can reach this address, so
        the token is what makes it ours. Compared in constant time, and a
        service started without one refuses every request rather than
        quietly accepting all of them.
        """
        if not expected:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE,
                                f"{RENDER_TOKEN_ENV} is not set, so this "
                                f"renderer will not answer anyone")
        if not secrets.compare_digest(token, expected):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED,
                                "bad or missing render token")

    @app.get("/health")
    async def health() -> dict:
        return {"ok": True, "version": __version__,
                "browsers_busy": pool.in_use, "browser_slots": pool.slots}

    @app.post("/render")
    def render(req: RenderRequest,
               x_render_token: str = Header(default="")) -> dict:
        authorise(x_render_token)
        try:
            with pool.slot():
                fetcher = pool.fetcher(max_scrolls=req.scroll)
                html = fetcher.fetch(req.url)
        except NoBrowserSlot as e:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(e),
                                headers={"Retry-After": "10"}) from e

        # None means robots said no, the address was refused, or the page
        # would not load. The caller treats all three the same way -- as no
        # content -- and saying which would leak more than it helps.
        return {"html": html}

    return app
