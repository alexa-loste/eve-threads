"""Claude-started threads.

Flow: claude_mcp.start_thread -> POST /start {name, suggested_urls} -> {id, go_url}
      -> Chrome opens go_url -> the extension sees a tab loading /go/{id},
      fetches GET /pending/{id} and starts recording under that name.

Claude's suggestions are NOT opened for the user. They are remembered, and when
the user opens one of them, the park marks that tab as suggested by Claude
(`is_suggested(url)`), which is how Eve learns which suggestions she picked.
POST /suggest adds more suggestions to the current thread as the chat goes on.
"""

from __future__ import annotations

import html
import time
import uuid
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel, Field

router = APIRouter()

_PENDING: dict[str, dict] = {}
_SUGGESTED: dict[str, str] = {}   # normalized url -> thread name
_CURRENT: dict = {"name": None}
_SUGGESTED_ORDER: list[str] = []
_CORTEX: dict[str, str] = {}      # thread name (lowercased) -> Eve cortex chosen for it


def cortex_for(thread_name: str) -> str | None:
    """The Eve cortex the user chose for this thread when Claude started it, if any."""
    return _CORTEX.get((thread_name or "").strip().lower())
_TTL_S = 600
BASE = "http://localhost:8799"


def _norm(url: str) -> str:
    """Match a clicked link to a suggestion despite scheme, www, trailing slash or #fragment."""
    p = urlsplit(url.strip())
    host = p.netloc.lower().removeprefix("www.")
    q = f"?{p.query}" if p.query else ""
    return f"{host}{p.path.rstrip('/')}{q}"


def is_suggested(url: str) -> bool:
    """True if Claude suggested this URL for a thread. For the park: set seeded=True."""
    return bool(url) and _norm(url) in _SUGGESTED


def _add(urls: list[str], name: str) -> list[str]:
    ok = [u for u in urls if u.startswith(("http://", "https://"))][:30]
    for u in ok:
        if _norm(u) not in _SUGGESTED:
            _SUGGESTED_ORDER.append(u)
        _SUGGESTED[_norm(u)] = name
    return ok


class StartIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    suggested_urls: list[str] = Field(default_factory=list, max_length=30)
    seed_urls: list[str] = Field(default_factory=list, max_length=30)  # old name, same meaning
    cortex: str | None = Field(default=None, max_length=100)


class SuggestIn(BaseModel):
    urls: list[str] = Field(min_length=1, max_length=30)


def _gc() -> None:
    now = time.time()
    for k in [k for k, v in _PENDING.items() if now - v["created"] > _TTL_S]:
        _PENDING.pop(k, None)


@router.post("/start")
def start(body: StartIn) -> dict:
    _gc()
    name = body.name.strip()
    urls = _add(body.suggested_urls + body.seed_urls, name)
    tid = uuid.uuid4().hex[:12]
    # seed_urls stays empty: the extension opens nothing; the user picks what to read.
    _PENDING[tid] = {"id": tid, "name": name, "seed_urls": [], "suggested_urls": urls,
                     "created": time.time()}
    _CURRENT["name"] = name
    if body.cortex and body.cortex.strip():
        _CORTEX[name.lower()] = body.cortex.strip()
    return {"cortex": cortex_for(name), "id": tid, "go_url": f"{BASE}/go/{tid}", "suggested_urls": urls}


@router.post("/claim")
def claim():
    """The extension claims the most recent unclaimed thread on the next page load."""
    _gc()
    open_reqs = [v for v in _PENDING.values() if not v.get("claimed")]
    if not open_reqs:
        return Response(status_code=204)
    req = max(open_reqs, key=lambda v: v["created"])
    for v in open_reqs:  # only the newest counts; older queued ones are superseded
        v["claimed"] = True
    return {"id": req["id"], "name": req["name"], "suggested_urls": req["suggested_urls"]}


@router.post("/suggest")
def suggest(body: SuggestIn) -> dict:
    name = _CURRENT["name"]
    if not name:
        raise HTTPException(409, "no thread started from Claude yet; call start_thread first")
    return {"thread": name, "added": _add(body.urls, name)}


@router.get("/pending/{tid}")
def pending(tid: str) -> dict:
    _gc()
    req = _PENDING.get(tid)
    if not req:
        raise HTTPException(404, "unknown or expired thread request")
    return {"id": req["id"], "name": req["name"], "seed_urls": [],
            "suggested_urls": req["suggested_urls"]}


def _links_html(urls: list[str]) -> str:
    if not urls:
        return "<p style='color:#888'>No suggestions from Claude yet.</p>"
    items = "".join(f'<li><a href="{html.escape(u)}" target="_blank" rel="noopener">{html.escape(u)}</a></li>'
                    for u in urls)
    return (f"<ul style='line-height:1.8'>{items}</ul>"
            f"<button id='open-all' data-urls='{html.escape(__import__('json').dumps(urls))}' "
            f"style='font:inherit;padding:.4rem .9rem'>Open all {len(urls)} in a tab group</button>")


@router.get("/suggestions", response_class=HTMLResponse)
def suggestions_page() -> str:
    """Claude links here ("see all suggestions"); the user reads first, then opens what they want."""
    name = _CURRENT["name"]
    urls = [u for u in _SUGGESTED_ORDER if _SUGGESTED.get(_norm(u)) == name] if name else []
    title = html.escape(name or "no thread yet")
    return f"""<!doctype html><meta charset="utf-8"><title>Suggestions: {title}</title>
<body style="font:16px system-ui;max-width:40rem;margin:10vh auto;color:#222">
<p style="color:#888;margin:0">eve-threads · suggested by Claude</p>
<h1 style="font-weight:600">{title}</h1>{_links_html(urls)}</body>"""


@router.get("/go/{tid}", response_class=HTMLResponse)
def go(tid: str) -> str:
    req = _PENDING.get(tid)
    name = html.escape(req["name"]) if req else "unknown thread"
    return f"""<!doctype html><meta charset="utf-8"><title>Recording: {name}</title>
<body style="font:16px system-ui;max-width:36rem;margin:15vh auto;color:#222">
<p style="color:#888;margin:0">eve-threads</p>
<h1 style="font-weight:600">Recording research thread: {name}</h1>
<p>Open whatever you want to read, including links Claude suggested.
Press <b>Cmd+Shift+E</b> to park the thread when you're done.</p></body>"""

_CORTEX_CACHE: dict = {"at": 0.0, "names": []}


@router.get("/cortexes")
def cortexes() -> dict:
    """The user's existing Eve cortexes, for the popup's picker. Cached 5 minutes."""
    if time.time() - _CORTEX_CACHE["at"] < 300 and _CORTEX_CACHE["names"]:
        return {"cortexes": _CORTEX_CACHE["names"], "default": "browsing"}
    names: list[str] = []
    try:
        import re
        import eve_sync
        url, default = eve_sync._config()

        async def fn(session):
            return await eve_sync._call(session, "eve_spaces", {})

        if url and eve_sync.TOKEN_FILE.exists():
            listing = eve_sync._run(eve_sync._with_session(url, False, fn))
            body = listing.split(":", 1)[1] if ":" in listing else listing
            body = re.split(r"\.\s+Ask\b", body)[0]
            for raw in body.split(","):
                n = re.sub(r"\s*\((main|shared)\)\s*$", "", raw.strip()).strip().rstrip(".")
                if n and n not in names:
                    names.append(n)
    except Exception:
        names = []
    if "browsing" not in names:
        names.insert(0, "browsing")
    _CORTEX_CACHE.update(at=time.time(), names=names)
    return {"cortexes": names, "default": "browsing"}
