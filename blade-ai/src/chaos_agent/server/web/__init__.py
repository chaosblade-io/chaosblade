"""Static hosting for the Web UI (``web/dist``) with SPA fallback.

Resolution mirrors the TS TUI bundle chain in ``chaos_agent.cli.main``
(env override → frozen bundle → wheel-embedded → in-tree dev build), so
every distribution form (PyInstaller binary / pip wheel / repo checkout)
finds its copy of the Web UI the same way the TUI bundle is found.

Mounted only when a built bundle resolves — a bare ``blade-ai server``
from a repo without ``web/dist`` keeps working, with ``GET /`` returning
an actionable JSON hint instead of a bare 404.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import JSONResponse


class SPAStaticFiles(StaticFiles):
    """StaticFiles that serves ``index.html`` for unknown paths.

    Client-side routes (TanStack Router: ``/tasks``, ``/replay``, …)
    don't exist on disk; without the fallback a refresh on any of them
    would 404. Real 404s for missing *assets* (``/assets/app-abc.js``)
    still 404 — the fallback only fires for extension-less paths, which
    is the SPA-route shape. (Heuristic mirrors what ``vite preview``
    and most SPA servers do.)
    """

    async def get_response(self, path: str, scope):  # type: ignore[no-untyped-def,override]
        try:
            return await super().get_response(path, scope)
        except StarletteHTTPException as exc:
            # Never swallow API misses: an unknown /api/* path must stay
            # a JSON 404, not become a 200 HTML page (which would make
            # client-side error handling see "success" with a body it
            # can't parse). The mount at "/" catches everything the
            # registered routers didn't, including API typos. The bare
            # "/api" path gets the same treatment (no trailing slash →
            # the startswith check alone would miss it).
            scope_path = scope.get("path", path)
            if (
                exc.status_code == 404
                and scope_path != "/api"
                and not scope_path.startswith("/api/")
                and "." not in path.rsplit("/", 1)[-1]
            ):
                # abort-safe: see invariants allowlist
                return await super().get_response("index.html", scope)
            raise


def resolve_web_dist() -> Path | None:
    """Locate a built Web UI bundle, or ``None`` when none exists.

    Search order:

      1. ``BLADE_AI_WEB_DIST`` env — explicit override (tests / dev).
         An invalid override fails closed (returns ``None``) rather
         than silently falling through to another copy — a typo'd
         explicit path should surface, not heal itself.
      2. PyInstaller frozen bundle (``sys._MEIPASS``) — blade-ai.spec
         embeds ``web/dist`` as ``chaos_agent/_web_assets``, so
         ``blade-ai web`` works out of the standalone binary.
      3. Wheel-embedded ``<chaos_agent>/_web_assets`` — hatch
         ``force-include`` target in pyproject.toml.
      4. ``<repo>/web/dist`` walked up from this file, stopping at the
         repo root (``pyproject.toml``) — the in-tree dev build
         (``npm --prefix web run build``).
    """
    override = os.environ.get("BLADE_AI_WEB_DIST")
    if override:
        candidate = Path(override).expanduser()
        return candidate if (candidate / "index.html").is_file() else None

    # PyInstaller onedir/onefile mode sets ``sys._MEIPASS`` to the data
    # root. Same explicit-first contract as _resolve_ts_bundle in
    # cli/main.py — don't rely on __file__ rewriting alone.
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        frozen = Path(meipass) / "chaos_agent" / "_web_assets"
        if (frozen / "index.html").is_file():
            return frozen

    here = Path(__file__).resolve()

    # ``parents[2]`` — this file is <pkg>/server/web/__init__.py, so
    # parents[0]=web, [1]=server, [2]=<pkg>. The wheel force-include
    # drops the bundle at <pkg>/_web_assets, i.e. parents[2].
    embedded = here.parents[2] / "_web_assets"
    if (embedded / "index.html").is_file():
        return embedded

    for parent in here.parents:
        candidate = parent / "web" / "dist"
        if (candidate / "index.html").is_file():
            return candidate
        if (parent / "pyproject.toml").is_file():
            break
    return None


def mount_web_ui(app: FastAPI) -> Path | None:
    """Mount the Web UI on ``app`` (last — API routes must match first).

    Starlette matches routes in registration order, so calling this
    after every ``include_router`` keeps ``/api/*`` ahead of the
    catch-all mount. Returns the mounted directory, or ``None`` when no
    bundle resolved (a JSON hint is served at ``GET /`` instead).
    """
    dist = resolve_web_dist()
    if dist is None:

        @app.get("/", include_in_schema=False)
        async def _web_ui_missing() -> JSONResponse:
            return JSONResponse(
                status_code=404,
                content={
                    "error": "web_ui_not_bundled",
                    "hint": (
                        "The Web UI bundle (web/dist) was not found. "
                        "Build it with `npm --prefix web run build`, or "
                        "install a blade-ai wheel/binary that embeds it."
                    ),
                },
            )

        return None

    app.mount("/", SPAStaticFiles(directory=dist, html=True), name="web-ui")
    return dist
