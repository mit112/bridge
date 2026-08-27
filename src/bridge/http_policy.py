"""How the app answers at the HTTP edge, before any route runs.

Static-asset caching, the fragment header the persistent shell swaps on, and
the host check that keeps a panel bound to 127.0.0.1 unreachable from a page
served by anything else. All of it is policy about a request rather than about
Bridge's data, which is why it sits outside the route module.
"""

from pathlib import Path

from fastapi import Request
from fastapi.staticfiles import StaticFiles


class CachedStaticFiles(StaticFiles):
    """`StaticFiles` that answers with a `Cache-Control`, which it otherwise omits.

    Starlette sends only `etag`/`last-modified`, which gives the browser no
    freshness lifetime at all -- so every navigation re-requests the
    render-blocking 95KB `/static/app.css` AND all six woff2 faces just to be
    told 304. Seven conditional round trips in front of first paint, for bytes
    already on disk.

    The max-ages are deliberately SHORT because these URLs are unversioned and
    Bridge is a local panel whose only user edits this CSS by hand. `immutable`
    or a multi-day age would mean an `app.css` edit stops appearing until a hard
    reload -- exactly the trap already recorded against this repo ("browsers
    cache app.css even though the server re-reads it"). A minute covers a burst
    of clicks through the nav and expires well inside an edit-and-reload cycle;
    `must-revalidate` forbids ever serving it stale past that.

    Fonts get a day: their content genuinely never changes -- a different weight
    is a different filename, so a stale hit is impossible rather than merely
    unlikely. Still revalidatable, not `immutable`, for the same reason as
    above: nothing here is worth a cache that cannot be cleared by a reload.
    """

    ASSET_CACHE_CONTROL = "public, max-age=60, must-revalidate"
    FONT_CACHE_CONTROL = "public, max-age=86400, must-revalidate"

    def file_response(self, full_path, stat_result, scope, status_code=200):
        response = super().file_response(full_path, stat_result, scope, status_code)
        # Set after `super()` so the 304 branch is covered too: `cache-control`
        # is one of the headers Starlette carries onto a `NotModifiedResponse`,
        # and a revalidation that answered without one would re-arm the same
        # header-less loop on the very next navigation.
        response.headers["cache-control"] = (
            self.FONT_CACHE_CONTROL
            if Path(full_path).suffix == ".woff2"
            else self.ASSET_CACHE_CONTROL
        )
        return response


FRAGMENT_HEADER = "x-bridge-fragment"

# The methods a cross-origin form post can reach with side effects. GET and
# HEAD are excluded because every one of Bridge's is a read.
UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

# The only hostnames a browser can address a panel bound to 127.0.0.1 with,
# absent an attacker-controlled DNS record. `::1` appears unbracketed because
# `_hostname` strips the brackets a Host header is required to carry.
LOOPBACK_HOSTNAMES = frozenset({"127.0.0.1", "localhost", "::1"})


def _hostname(host: str) -> str:
    """The host part of a `Host` header, with any port removed.

    Split on the LAST colon and only when what follows is a port, so
    `evil.example:127.0.0.1` does not read as the host `evil.example:127.0.0.1`
    being compared away to something loopback-looking. IPv6 is only recognised
    bracketed, which is what RFC 3986 requires of a Host header anyway.
    """
    if host.startswith("["):
        return host.partition("]")[0].lstrip("[").lower()
    name, sep, port = host.rpartition(":")
    return name.lower() if sep and port.isdigit() else host.lower()


def _layout_for(request: Request) -> str:
    """Which layout a page template extends.

    A request without the header renders exactly what it always did, which is
    what keeps the existing route tests a true statement about the app.
    """
    return "_fragment.html" if request.headers.get(FRAGMENT_HEADER) else "base.html"
