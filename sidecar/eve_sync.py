"""Sync a parked browsing thread into Eve, through Eve's public MCP interface.

Contract (called by the sidecar):

    sync_thread(thread: dict, markdown: str) -> dict
        {"synced": True, "memory_id": <Eve write receipt>, "space": ...}
        {"synced": False, "reason": "..."}   # never raises; local-only still works

Configuration (sidecar/.env, gitignored):

    EVE_MCP_URL   the MCP endpoint from Eve's docs / in-app connect card
    EVE_SPACE     cortex to write into (default "browsing"; created if missing)

Auth is standard MCP OAuth (discovery, dynamic client registration, PKCE).
Sign in once, in your own browser:

    .venv/bin/python -m eve_sync login

Tokens are cached in sidecar/.eve_token.json (gitignored). No token ever goes in
.env, on a command line, or in this repo. `sync_thread` never opens a browser:
if you are not signed in it returns {"synced": False, ...}.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
import os
import re
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

HERE = Path(__file__).resolve().parent
TOKEN_FILE = HERE / ".eve_token.json"
CALLBACK_PORT = int(os.getenv("EVE_CALLBACK_PORT", "8798"))
REDIRECT_URI = f"http://127.0.0.1:{CALLBACK_PORT}/callback"
DEFAULT_SPACE = "browsing"
MAX_MEMORY_CHARS = 3500


def _load_dotenv() -> None:
    env = HERE / ".env"
    if not env.exists():
        return
    for line in env.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def _config() -> tuple[str | None, str]:
    _load_dotenv()
    return os.getenv("EVE_MCP_URL") or None, os.getenv("EVE_SPACE") or DEFAULT_SPACE


# ---------------------------------------------------------------- token cache

class FileTokenStorage:
    """mcp TokenStorage backed by a local JSON file (mode 600)."""

    def __init__(self, path: Path = TOKEN_FILE):
        self.path = path

    def _read(self) -> dict:
        try:
            return json.loads(self.path.read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def _write(self, data: dict) -> None:
        self.path.write_text(json.dumps(data))
        os.chmod(self.path, 0o600)

    async def get_tokens(self):
        from mcp.shared.auth import OAuthToken
        t = self._read().get("tokens")
        return OAuthToken.model_validate(t) if t else None

    async def set_tokens(self, tokens) -> None:
        d = self._read()
        d["tokens"] = tokens.model_dump(mode="json", exclude_none=True)
        self._write(d)

    async def get_client_info(self):
        from mcp.shared.auth import OAuthClientInformationFull
        c = self._read().get("client")
        return OAuthClientInformationFull.model_validate(c) if c else None

    async def set_client_info(self, client_info) -> None:
        d = self._read()
        d["client"] = client_info.model_dump(mode="json", exclude_none=True)
        self._write(d)


# ---------------------------------------------------------------- MCP session

class NotSignedIn(Exception):
    pass


def _oauth(url: str, interactive: bool):
    from mcp.client.auth import OAuthClientProvider
    from mcp.shared.auth import AuthorizationCodeResult, OAuthClientMetadata

    metadata = OAuthClientMetadata(
        client_name="eve-threads",
        redirect_uris=[REDIRECT_URI],
        grant_types=["authorization_code", "refresh_token"],
        response_types=["code"],
        token_endpoint_auth_method="none",
    )
    result: dict = {}
    got = threading.Event()

    async def redirect_handler(auth_url: str) -> None:
        if not interactive:
            raise NotSignedIn("not signed in to Eve: run `python -m eve_sync login`")
        _start_callback_server(result, got)
        print(f"\nOpening your browser to sign in to Eve:\n  {auth_url}\n")
        webbrowser.open(auth_url)

    async def callback_handler():
        for _ in range(600):  # up to 5 minutes, without pinning a thread
            if got.is_set():
                break
            await asyncio.sleep(0.5)
        if "code" not in result:
            raise NotSignedIn(result.get("error") or "sign-in timed out")
        return AuthorizationCodeResult(code=result["code"], state=result.get("state"),
                                       iss=result.get("iss"))

    return OAuthClientProvider(
        server_url=url,
        client_metadata=metadata,
        storage=FileTokenStorage(),
        redirect_handler=redirect_handler,
        callback_handler=callback_handler,
    )


def _start_callback_server(result: dict, got: threading.Event) -> None:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            q = parse_qs(urlparse(self.path).query)
            for k in ("code", "state", "iss", "error"):
                if k in q:
                    result[k] = q[k][0]
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"<p>Signed in to Eve. You can close this tab.</p>")
            got.set()

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", CALLBACK_PORT), Handler)
    threading.Thread(target=srv.handle_request, daemon=True).start()


async def _refresh_if_stale(url: str) -> None:
    """Refresh the cached access token ourselves when it is near expiry.

    The SDK does not know when a token loaded from disk was issued, so an expired one
    looks valid, gets a 401, and the SDK falls through to a browser sign-in (which a
    background park cannot do). Refreshing by file age avoids that.
    """
    import httpx
    try:
        data = json.loads(TOKEN_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return
    tok, client = data.get("tokens") or {}, data.get("client") or {}
    age = time.time() - TOKEN_FILE.stat().st_mtime
    if not tok.get("refresh_token") or age < int(tok.get("expires_in") or 3600) - 120:
        return
    host = url.split("/eve-mcp/")[0]
    async with httpx.AsyncClient(timeout=15) as http:
        meta = (await http.get(f"{host}/.well-known/oauth-authorization-server/eve-mcp")).json()
        form = {"grant_type": "refresh_token", "refresh_token": tok["refresh_token"],
                "client_id": client.get("client_id", "")}
        if client.get("client_secret"):
            form["client_secret"] = client["client_secret"]
        r = await http.post(meta.get("token_endpoint") or f"{host}/eve-mcp/token", data=form)
    if r.status_code != 200:
        raise NotSignedIn(f"Eve token refresh failed ({r.status_code}): run `python -m eve_sync login`")
    new = r.json()
    new.setdefault("refresh_token", tok["refresh_token"])
    data["tokens"] = {k: v for k, v in new.items() if v is not None}
    TOKEN_FILE.write_text(json.dumps(data))
    os.chmod(TOKEN_FILE, 0o600)


async def _with_session(url: str, interactive: bool, fn):
    from mcp import ClientSession
    from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client

    await _refresh_if_stale(url)
    auth = _oauth(url, interactive)
    async with create_mcp_http_client(auth=auth) as http:
        async with streamable_http_client(url, http_client=http) as streams:
            read, write = streams[0], streams[1]
            async with ClientSession(read, write) as session:
                await session.initialize()
                return await fn(session)


async def _call(session, name: str, args: dict) -> str:
    res = await session.call_tool(name, args)
    text = "\n".join(getattr(c, "text", "") for c in (getattr(res, "content", None) or []))
    if getattr(res, "isError", False):
        raise RuntimeError(f"{name} failed: {text[:300]}")
    return text


async def _space_exists(session, space: str) -> bool:
    listing = await _call(session, "eve_spaces", {})
    names = re.split(r"[,:]\s*", listing)
    return any(re.fullmatch(rf"{re.escape(space)}( \(.*\))?\.?", n.strip(), re.I) for n in names)


async def _ensure_space(session, space: str) -> None:
    if not await _space_exists(session, space):
        await _call(session, "eve_spaces", {"create": space})


# ---------------------------------------------------------------- the memory

def _title(tab: dict) -> str:
    return (tab.get("title") or tab.get("url") or "untitled").strip()


def compose_memory(thread: dict) -> str:
    """One self-contained paragraph Eve can recall by topic ("voter bias research thread")."""
    name = thread.get("name") or "untitled"
    # Off-topic tabs never reach Eve: they are not part of the thread.
    tabs = [dict(t, status="unread") if (t.get("marked_later") or t.get("later")) and t.get("status") != "off_topic" else t
            for t in (thread.get("tabs") or []) if t.get("status") not in ("off_topic", "routed")]
    routed = [t for t in (thread.get("tabs") or []) if t.get("status") == "routed"]
    by = {s: [t for t in tabs if t.get("status") == s]
          for s in ("read", "skimmed", "unread", "person_org")}
    started = (thread.get("started_at") or "")[:16].replace("T", " ")
    parked = (thread.get("parked_at") or "")[:16].replace("T", " ")

    when = f"browsed {started} to {parked} UTC, " if started and parked else ""
    parts = [f'Alexa\'s research browsing thread "{name}" '
             f"({when}{len(tabs)} tab{'' if len(tabs) == 1 else 's'}, parked by eve-threads)."]
    seeded = [t for t in tabs if t.get("seeded")]
    if seeded:
        parts.append(f"Claude started this thread and suggested {len(seeded)} source(s); she read "
                     f"{sum(t.get('status') == 'read' for t in seeded)} of them.")
    if by["read"]:
        parts.append("Read closely (part of what she already knows; do not re-suggest): "
                     + "; ".join(f"{_title(t)}" for t in by["read"]) + ".")
    if by["skimmed"]:
        parts.append("Skimmed: " + "; ".join(
            f"{_title(t)}" for t in by["skimmed"]) + ".")
    if by["person_org"]:
        parts.append("People and organizations looked up: " + "; ".join(
            f"{_title(t)}" for t in by["person_org"]) + ".")
    if by["unread"]:
        parts.append("Still unread (the queue to pick up next): " + "; ".join(
            f"{_title(t)}{' (marked to read later)' if (t.get('marked_later') or t.get('later')) else ''} <{t.get('url')}>"
            for t in by["unread"]) + ".")
    focused = [t for t in tabs if t.get("status") != "unread"]
    if focused:
        last = focused[-1]
        parts.append(f"She stopped at: {_title(last)} <{last.get('url')}>.")

    if routed:
        parts.append("Filed under her other threads: " + "; ".join(
            f'{_title(t)} (under "{(t.get("routed_to") or {}).get("name", "another thread")}")'
            for t in routed) + ".")
    quotes = [(t, h) for t in tabs for h in _highlights(t)][:6]
    if quotes:
        parts.append("She highlighted: " + " ".join(
            f'"{h}" (in {_title(t)})' for t, h in quotes))

    text = " ".join(parts)
    if len(text) > MAX_MEMORY_CHARS:
        text = text[: MAX_MEMORY_CHARS - 40].rsplit(";", 1)[0] + "; (list truncated)."
    return text


def _highlights(t: dict) -> list[str]:
    out = []
    for h in (t.get("highlight_texts") or [])[:10]:
        h = " ".join(str(h).split())[:300]
        if h:
            out.append(h)
    return out


def _mins(s) -> str:
    s = int(s or 0)
    return f"{s // 60} min {s % 60} s" if s >= 60 else f"{s} s"


def compose_read_cards(thread: dict) -> list[tuple[str, str, str]]:
    """One source card per on-topic tab, with its URL attached (Eve shows it under "Where").

    Tabs read closely are marked as part of Alexa's knowledge, not just history."""
    name = thread.get("name") or "untitled"
    cards = []
    for t in thread.get("tabs") or []:
        status = t.get("status")
        if status == "off_topic" or not t.get("url"):
            continue
        if status == "routed":
            title = _title(t)
            to = (t.get("routed_to") or {}).get("name") or "another thread"
            text = (f'Source document "{title}", a web page Alexa opened while researching '
                    f'"{name}" that belongs to her "{to}" research thread, so it was filed there.')
            cards.append((text, t["url"], title[:120], True))
            continue
        if (t.get("marked_later") or t.get("later")):
            status = "unread"
        title = _title(t)
        hl = int(t.get("highlights") or 0)
        detail = f"focused {_mins(t.get('focus_s'))}, scrolled {round(100 * float(t.get('scroll_depth') or 0))}%"
        if hl:
            detail += f", {hl} highlight{'s' if hl != 1 else ''}"
        # "Source document ..., a web page ..." makes Eve file it as a Document, so the
        # attached URL shows under "Where" on the source's own card (tested 2026-09-24).
        if status == "read":
            text = (f'Source document "{title}", a web page Alexa has read closely ({detail}) '
                    f'while researching "{name}". It is part of what she already knows on this '
                    f"topic: do not suggest it to her as new reading.")
        elif status == "unread":
            text = (f'Source document "{title}", a web page in Alexa\'s unread queue for her '
                    f'"{name}" research thread: '
                    + ("she marked it to read later." if (t.get("marked_later") or t.get("later")) else "opened but not read yet."))
        elif status == "person_org":
            text = (f'Source document "{title}", a web page about a person or organization '
                    f'Alexa looked up while researching "{name}".')
        else:
            text = (f'Source document "{title}", a web page Alexa skimmed ({detail}) while '
                    f'researching "{name}".')
        if t.get("seeded"):
            text += (" Claude suggested this source when it started the thread"
                     + (", and she read it." if status == "read" else
                        ", and she has not read it yet." if status == "unread" else "."))
        abstract = " ".join(str(t.get("description") or t.get("abstract") or "").split())[:600]
        hls = _highlights(t)
        if abstract:
            text += f" About: {abstract}"
        if hls:
            text += " She highlighted: " + " ".join(f'"{h}"' for h in hls)
        # Only a close read or highlights earns its own extra memory; every other source
        # just gets its link pinned onto Eve's card (each extra memory is a slow encode).
        cards.append((text, t["url"], title[:120], bool(hls) or status == "read"))
    return cards


_ROW = re.compile(r"^\s*\d+\. \[(\w+)\] (.+?) — .*?⟨id: ([0-9a-f-]{8,})⟩(.*)$")


def _norm(s: str) -> set[str]:
    s = re.split(r"\s[|·–-]\s(?=[^|·–-]*$)", s)[0]  # drop a trailing " | Site" / " - Site"
    return {w for w in re.findall(r"[a-z0-9]+", s.lower()) if len(w) > 2}


async def _find_doc(session, space: str, title: str) -> str | None:
    """The un-pinned Document card Eve made for this source, if there is one."""
    want = _norm(title)
    if not want:
        return None
    text = await _call(session, "eve_recall", {"query": title, "space": space, "limit": 6})
    best, best_score = None, 0.0
    for line in text.splitlines():
        m = _ROW.match(line)
        if not m or m.group(1) != "Document" or "📎" in m.group(4):
            continue
        got = _norm(m.group(2))
        score = len(want & got) / max(1, len(want | got))
        if score > best_score:
            best, best_score = m.group(3), score
    return best if best_score >= 0.6 else None


# ---------------------------------------------------------------- public API

def _run(coro):
    """Run a coroutine whether or not the caller is already inside an event loop."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    with concurrent.futures.ThreadPoolExecutor(1) as ex:
        return ex.submit(asyncio.run, coro).result()


def sync_thread(thread: dict, markdown: str) -> dict:
    url, space = _config()
    if not url:
        return {"synced": False, "reason": "EVE_MCP_URL not set in sidecar/.env"}
    if not TOKEN_FILE.exists():
        return {"synced": False, "reason": "not signed in to Eve: run `python -m eve_sync login`"}

    text = compose_memory(thread)
    read_cards = compose_read_cards(thread)
    chosen = thread.get("space") or thread.get("cortex")
    if not chosen:
        try:
            from start_queue import cortex_for
            chosen = cortex_for(thread.get("name") or "")
        except Exception:
            chosen = None
    default_space = space
    if chosen:
        space = chosen

    async def write(session):
        nonlocal space
        if space == default_space:
            await _ensure_space(session, space)
        elif not await _space_exists(session, space):
            space = default_space  # never create a cortex from a typo; fall back
            await _ensure_space(session, space)
        out = await _call(session, "eve_remember", {
            "text": text,
            "space": space,
            "wait": True,
            "source": "eve-threads browser extension",
        })
        # Eve files each source named in the thread as its own Document card. Pin the URL
        # onto THAT card (shows under "Where"); only if none is found, write our own card.
        for card, url, title, rich in read_cards:
            try:
                doc_id = await _find_doc(session, space, title)
                if doc_id:
                    await _call(session, "eve_attach", {
                        "memory_id": doc_id, "url": url, "title": title, "space": space})
                if rich or not doc_id:  # a card with abstract/highlights carries content Eve lacks
                    await _call(session, "eve_remember", {
                        "text": card, "space": space, "wait": False,
                        "source": "eve-threads browser extension",
                        "attach_url": url, "attach_title": title,
                    })
            except Exception:  # a card failing never fails the thread
                pass
        return out

    try:
        out = _run(_with_session(url, interactive=False, fn=write))
    except BaseException as e:  # noqa: BLE001 - the demo must never crash on sync
        if isinstance(e, KeyboardInterrupt):
            raise
        return {"synced": False, "reason": f"{type(e).__name__}: {e}"[:300]}

    m = re.search(r"[Rr]eceipt\s+([0-9a-f]{8,})", out)
    return {"synced": True, "memory_id": m.group(1) if m else None, "space": space,
            "eve_reply": out[:200]}


def login() -> None:
    url, space = _config()
    if not url:
        sys.exit("Set EVE_MCP_URL in sidecar/.env first.")

    async def check(session):
        await _ensure_space(session, space)
        return await _call(session, "eve_spaces", {})

    _run(_with_session(url, interactive=True, fn=check))
    print(f"Signed in. Threads will sync to the '{space}' cortex.")


if __name__ == "__main__":
    if sys.argv[1:] == ["login"]:
        login()
    elif sys.argv[1:] == ["test"]:
        demo = {"thread_id": "t-test", "name": "eve-threads sync test",
                "started_at": "2026-09-24T18:00:00Z", "parked_at": "2026-09-24T18:30:00Z",
                "tabs": [{"url": "https://example.com/a", "title": "Example A", "opener_url": None,
                          "status": "read", "focus_s": 120, "scroll_depth": 0.9, "highlights": 1},
                         {"url": "https://example.com/b", "title": "Example B",
                          "opener_url": "https://example.com/a", "status": "unread",
                          "focus_s": 0, "scroll_depth": 0.0, "highlights": 0}]}
        print(compose_memory(demo))
        print(sync_thread(demo, ""))
    else:
        sys.exit("usage: python -m eve_sync login | test")
