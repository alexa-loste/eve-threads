"""eve-threads local sidecar.

Runs on localhost only. Receives a parked research thread from the extension,
leaves out tabs that aren't about the thread's topic (using a small on-device
embedding model), writes a plain markdown file you own, and (if configured)
syncs a summary to your Eve memory.

Run:  ./run.sh     (or: .venv/bin/uvicorn server:app --port 8799)
"""
import os
import re
import sys
import time
import pathlib
import datetime
import json
import threading

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

EMBED_MODEL = "sentence-transformers/all-MiniLM-L6-v2"
# If the model is already downloaded, don't let a network check on bad wifi stall startup.
# (Must be set before huggingface_hub is imported.)
_hf_home = pathlib.Path(os.environ.get("HF_HOME", pathlib.Path.home() / ".cache" / "huggingface"))
if (_hf_home / "hub" / ("models--" + EMBED_MODEL.replace("/", "--"))).exists():
    os.environ.setdefault("HF_HUB_OFFLINE", "1")


def _load_env():
    p = HERE / ".env"
    if not p.exists():
        return
    for line in p.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_env()

from fastapi import FastAPI  # noqa: E402
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402

THREADS_DIR = pathlib.Path(os.environ.get("EVE_THREADS_DIR", HERE.parent / "threads")).expanduser()
# Cosine similarity between a tab's title and the thread topic, below which the tab is
# left out as off-topic. Deliberately low: in testing, clear junk (email, shopping, a hotel
# page, an unrelated doc) scored <= 0.10, while the least similar genuinely on-topic pages
# scored ~0.20. Keeping a marginal page is better than hiding a relevant one.
TOPIC_MIN_SIM = float(os.environ.get("TOPIC_MIN_SIM", "0.15"))

app = FastAPI(title="eve-threads sidecar")
# Only the extension (chrome-extension://...) and local tools (no Origin header) may call
# the sidecar. Without this, any website you visit could post threads into your memory or
# make /start open URLs in your browser.
app.add_middleware(CORSMiddleware, allow_origin_regex=r"chrome-extension://.*",
                   allow_methods=["*"], allow_headers=["*"])


@app.middleware("http")
async def _no_web_pages(request, call_next):
    origin = request.headers.get("origin", "")
    if origin.startswith(("http://", "https://")):
        from fastapi.responses import JSONResponse
        return JSONResponse({"error": "web pages can't call the eve-threads sidecar"}, status_code=403)
    return await call_next(request)

# Optional: routes that let Claude start a thread (POST /start, GET /go/{id}, GET /pending/{id}).
try:
    from start_queue import router as _start_router
    app.include_router(_start_router)
except ImportError:
    pass

# ---------- on-device topic model ----------

embedder = None
embed_state = "loading"


def _load_embedder():
    global embedder, embed_state
    try:
        from sentence_transformers import SentenceTransformer
        embedder = SentenceTransformer(EMBED_MODEL)
        embed_state = "ready"
    except Exception as e:  # keep running without the off-topic filter
        embed_state = f"unavailable ({type(e).__name__})"


threading.Thread(target=_load_embedder, daemon=True).start()


# An off-topic tab is filed under another thread instead when it clearly belongs there:
# similarity to that thread's topic >= ROUTE_MIN_SIM, and well above its similarity here.
# (A Trump–Xi article scored ~0.6 against "trump and xi meeting" and ~0.1 against an
# unrelated topic in testing.)
ROUTE_MIN_SIM = float(os.environ.get("ROUTE_MIN_SIM", "0.35"))
ROUTE_MARGIN = 0.15


# Communication apps are never research sources, and their titles can contain personal
# details (an inbox title includes the email address), so they're always left out and never synced.
APP_URL = re.compile(r"^https?://([a-z0-9-]+\.)*(mail\.google\.com|calendar\.google\.com|messages\.google\.com|"
                     r"outlook\.(live|office|office365)\.com|mail\.yahoo\.com|app\.slack\.com|"
                     r"web\.whatsapp\.com|discord\.com|messenger\.com|web\.telegram\.org|app\.superhuman\.com)(/|$)", re.I)


def classify(tabs, topic, others=None):
    """Read/skimmed/unread comes from reading signals (the extension's heuristic).
    Every tab except ones Claude suggested or opened, or the user marked for later, is
    compared with the thread topic. Tabs below TOPIC_MIN_SIM are off-topic; an off-topic
    tab that clearly matches another parked thread (`others`) is routed there."""
    apps = 0
    for t in tabs:
        if APP_URL.match(t["url"]):
            t["status"] = "off_topic"
            apps += 1
    if embedder is None:
        return f"reading signals only (topic model {embed_state})"
    check = [t for t in tabs if t["status"] != "off_topic" and not t.get("seeded") and not t.get("later")
             and "claude.ai" not in (t.get("opener_url") or "") and "claude.ai" not in t["url"]]
    if not check:
        return "reading signals decided read/skimmed/unread"
    vecs = embedder.encode([topic] + [t["title"] for t in check], normalize_embeddings=True)
    off = []
    for t, v in zip(check, vecs[1:]):
        sim = float(vecs[0] @ v)
        t["topic_similarity"] = round(sim, 2)
        if sim < TOPIC_MIN_SIM:
            t["status"] = "off_topic"
            off.append((t, v))

    routed = 0
    others = [o for o in (others or []) if o["name"].strip().lower() != topic.strip().lower()]
    if off and others:
        ovecs = embedder.encode([o["name"] for o in others], normalize_embeddings=True)
        for t, v in off:
            sims = ovecs @ v
            j = int(sims.argmax())
            best = float(sims[j])
            if best >= ROUTE_MIN_SIM and best >= t["topic_similarity"] + ROUTE_MARGIN:
                t["status"] = "routed"
                t["routed_to"] = {"thread_id": others[j]["thread_id"], "name": others[j]["name"]}
                t["route_similarity"] = round(best, 2)
                routed += 1
    return (f"on-device embedding model left out {len(off) - routed + apps} off-topic tab(s) and filed "
            f"{routed} under other threads; reading signals decided read/skimmed/unread")


# ---------- markdown you own ----------

SECTIONS = [
    ("read", "Read"),
    ("skimmed", "Skimmed"),
    ("unread", "Unread: reading queue"),
    ("person_org", "People and orgs"),
]


def _dur(s):
    return f"{s // 60} min {s % 60} s" if s >= 60 else f"{s} seconds"


def _when(iso):
    return datetime.datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone().strftime("%b %-d, %Y %-I:%M %p")


def _host(url):
    m = re.match(r"https?://([^/]+)", url or "")
    return m.group(1) if m else url


def render(thread, classifier_note):
    tabs = thread["tabs"]
    kept = [t for t in tabs if t["status"] not in ("off_topic", "routed")]
    lines = [
        f"# Research thread: {thread['name']}",
        "",
        f"Started {_when(thread['started_at'])} · parked {_when(thread['parked_at'])} · {len(kept)} tab{'' if len(kept) == 1 else 's'}"
        + (f" · Eve cortex: {thread['cortex']}" if thread.get("cortex") else ""),
        "",
    ]
    for key, label in SECTIONS:
        items = [t for t in tabs if t["status"] == key]
        if not items:
            continue
        lines.append(f"## {label}")
        for t in items:
            sig = f"focused {_dur(t['focus_s'])}, scrolled {round(t['scroll_depth'] * 100)}%"
            if t.get("highlights"):
                sig += f", {t['highlights']} highlight(s)"
            src = (" (suggested by Claude)" if t.get("seeded")
                   else f" (opened from {_host(t['opener_url'])})" if t.get("opener_url") else "")
            later = " · marked for later" if t.get("later") else ""
            lines.append(f"- [{t['title']}]({t['url']}): {sig}{src}{later}")
            if t.get("abstract"):
                lines.append(f"  - About: {t['abstract']}")
            for h in t.get("highlight_texts") or []:
                lines.append(f"  - Highlighted: \u201c{h}\u201d")
        lines.append("")
    last = thread.get("last_focused_url")
    if last:
        title = next((t["title"] for t in tabs if t["url"] == last), last)
        lines += ["## Where I stopped", f"- [{title}]({last})", ""]
    routed = [t for t in tabs if t["status"] == "routed"]
    if routed:
        lines.append("## Filed under other threads")
        lines += [f"- [{t['title']}]({t['url']}) → \"{t['routed_to']['name']}\" (similarity {t['route_similarity']})"
                  for t in routed]
        lines.append("")
    off = [t for t in tabs if t["status"] == "off_topic"]
    if off:
        lines.append("## Left out as off-topic (not synced)")
        for t in off:
            if APP_URL.match(t["url"]):  # don't write inbox/chat titles anywhere
                lines.append(f"- {_host(t['url'])} (communication app, not recorded)")
            else:
                lines.append(f"- [{t['title']}]({t['url']}): topic similarity {t.get('topic_similarity', '?')}")
        lines.append("")
    lines += [
        "---",
        f"_Captured on this machine by eve-threads. Classification: {classifier_note}._",
    ]
    return "\n".join(lines) + "\n"


def _slug(s):
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")[:60] or "thread"


# ---------- API ----------

def _eve_sync_available():
    return (HERE / "eve_sync.py").exists() or (HERE / "eve_sync").is_dir()


@app.get("/health")
def health():
    return {
        "topic_model": embed_state,
        "threads_dir": str(THREADS_DIR),
        "eve_sync": "installed" if _eve_sync_available() else "not installed",
    }


# One park at a time: FastAPI runs sync endpoints on a thread pool, and PyTorch's
# Apple-GPU (MPS) backend can abort the whole process if two threads run a model at once.
_park_lock = threading.Lock()
_parked = {}  # thread_id -> result, so a repeated park doesn't write a second file or Eve memory


@app.post("/park")
def park(thread: dict):
    with _park_lock:
        tid = thread.get("thread_id")
        if tid in _parked:
            return {**_parked[tid], "duplicate": True}
        topics = [tp for tp in (thread.get("topics") or []) if tp.get("name")]
        if len(topics) > 1:
            result = _park_multi(thread, topics)
        else:
            if topics:
                thread["name"] = topics[0]["name"]
                thread["cortex"] = topics[0].get("cortex") or thread.get("cortex")
                thread["space"] = thread["cortex"]  # eve_sync reads "space"
            thread.pop("topics", None)
            result = _park(thread)
        if tid:
            _parked[tid] = result
        return result


# ---------- several topics at once ----------

def _assign(tabs, names):
    """Map each tab to the index of its best-matching topic, or -1 for none. Tabs Claude
    suggested or opened, or marked read-later, always go to their closest topic."""
    if embedder is None or not names:
        return [0] * len(tabs)
    if not tabs:
        return []
    nv = embedder.encode(names, normalize_embeddings=True)
    tv = embedder.encode([t.get("title") or t.get("url") or "" for t in tabs], normalize_embeddings=True)
    out = []
    for t, v in zip(tabs, tv):
        if APP_URL.match(t.get("url") or ""):
            out.append(-1)
            continue
        sims = nv @ v
        j = int(sims.argmax())
        forced = t.get("seeded") or t.get("later") or "claude.ai" in (t.get("opener_url") or "")
        out.append(j if forced or float(sims[j]) >= TOPIC_MIN_SIM else -1)
    return out


@app.post("/route")
def route(body: dict):
    """Live grouping for the popup: {topics: [name], tabs: [{url, title, ...}]} -> {url: topic index or -1}."""
    tabs = body.get("tabs") or []
    with _park_lock:  # one model call at a time
        groups = _assign(tabs, body.get("topics") or [])
    return {"groups": {t["url"]: i for t, i in zip(tabs, groups)}}


def _park_multi(thread, topics):
    """Split a session with several topics into one thread per topic, then park each."""
    tabs = thread["tabs"]
    _mark_suggested(tabs)
    groups = _assign(tabs, [tp["name"] for tp in topics])
    results = []
    for i, tp in enumerate(topics):
        # tabs that match no topic go with the first one, so they're listed (and routed) once
        sub_tabs = [t for t, g in zip(tabs, groups) if g == i or (g == -1 and i == 0)]
        if not sub_tabs:
            continue
        urls = {t["url"] for t in sub_tabs}
        sub = {k: v for k, v in thread.items() if k != "topics"}
        sub.update({
            "thread_id": f"{thread.get('thread_id', 'x')}-{tp.get('id', i)}",
            "name": tp["name"],
            "cortex": tp.get("cortex") or thread.get("cortex"),
            "space": tp.get("cortex") or thread.get("cortex"),  # eve_sync reads "space"
            "started_at": tp.get("started_at") or thread["started_at"],
            "tabs": sub_tabs,
            "focus_order": [u for u in thread.get("focus_order") or [] if u in urls],
            "last_focused_url": thread.get("last_focused_url") if thread.get("last_focused_url") in urls else None,
        })
        results.append(_park(sub))
    if not results:
        return _park({k: v for k, v in thread.items() if k != "topics"})
    return {
        "path": results[0]["path"],
        "paths": [r["path"] for r in results],
        "threads": len(results),
        "classifier": results[0]["classifier"],
        "statuses": {u: s for r in results for u, s in r["statuses"].items()},
        "close_urls": [u for r in results for u in r.get("close_urls", [])],
        "synced": any(r.get("synced") for r in results),
        "reason": "; ".join(r["reason"] for r in results if r.get("reason")) or None,
    }


def _last_on_topic(thread):
    """The last tab the user focused that wasn't left out as off-topic."""
    off = {t["url"] for t in thread["tabs"] if t["status"] in ("off_topic", "routed")}
    kept = {t["url"] for t in thread["tabs"]} - off
    for url in reversed(thread.get("focus_order") or []):
        if url in kept:
            return url
    last = thread.get("last_focused_url")
    return last if last in kept else None


INDEX_NAME = ".index.json"  # thread_id -> {thread_id, name, path, parked_at}


def _load_index():
    idx = {}
    try:
        idx = {e["thread_id"]: e for e in json.loads((THREADS_DIR / INDEX_NAME).read_text())}
    except Exception:
        pass
    known = {e["path"] for e in idx.values()}
    for p in sorted(THREADS_DIR.glob("*.md"), key=lambda p: p.stat().st_mtime):  # threads parked before the index
        if str(p) in known:
            continue
        m = re.match(r"# Research thread: (.+)", p.read_text().split("\n", 1)[0])
        if m:
            tid = p.stem.rsplit("-", 1)[-1]
            idx[tid] = {"thread_id": tid, "name": m.group(1).strip(), "path": str(p), "parked_at": None}
    return idx


def _others(idx, this_id):
    """Other parked threads, one per topic name (the most recent)."""
    by_name = {}
    for e in idx.values():
        if e["thread_id"] != this_id and pathlib.Path(e["path"]).exists():
            by_name[e["name"].strip().lower()] = e  # later entries win
    return list(by_name.values())


def _append_routed(thread, routed, idx):
    for t in routed:
        target = idx.get(t["routed_to"]["thread_id"])
        if not target:
            continue
        with open(target["path"], "a") as f:
            f.write(f"\n## Added from \"{thread['name']}\" ({_when(thread['parked_at'])})\n"
                    f"- [{t['title']}]({t['url']}): focused {_dur(t['focus_s'])}, "
                    f"scrolled {round(t['scroll_depth'] * 100)}%, similarity {t['route_similarity']}\n")


# Closing needs more confidence than keeping. A borderline tab (similarity between
# TOPIC_MIN_SIM and CLOSE_MIN_SIM) is saved to the thread but left open, so parking never
# closes something unrelated the user still wants to look at.
CLOSE_MIN_SIM = float(os.environ.get("CLOSE_MIN_SIM", "0.30"))


def _close_urls(thread):
    urls = {t["url"] for t in thread["tabs"]}
    close = []
    for t in thread["tabs"]:
        if t["status"] in ("off_topic", "routed"):
            continue
        opener = t.get("opener_url") or ""
        connected = opener in urls or "claude.ai" in opener or "claude.ai" in t["url"]
        sim = t.get("topic_similarity")
        if t.get("seeded") or t.get("later") or connected or (sim is not None and sim >= CLOSE_MIN_SIM):
            close.append(t["url"])
    return close


def _mark_suggested(tabs):
    """A link clicked in the Claude app arrives in Chrome with no opener, so match URLs
    against the sources Claude suggested (start_queue keeps that list)."""
    try:
        from start_queue import is_suggested
    except ImportError:
        return
    for t in tabs:
        try:
            if is_suggested(t["url"]):
                t["seeded"] = True
        except Exception:
            pass


def _park(thread):
    THREADS_DIR.mkdir(parents=True, exist_ok=True)
    _mark_suggested(thread["tabs"])  # before classify: Claude's suggestions skip the off-topic check
    idx = _load_index()
    this_id = str(thread.get("thread_id", ""))
    note = classify(thread["tabs"], thread["name"], _others(idx, this_id))
    thread["last_focused_url"] = _last_on_topic(thread)  # eve_sync reads this too
    statuses = {t["url"]: t["status"] for t in thread["tabs"]}
    md = render(thread, note)

    stamp = datetime.datetime.now().strftime("%Y-%m-%d-%H%M")
    tid = re.sub(r"[^a-z0-9]", "", this_id.lower())[-6:] or "x"  # end of id: unique per topic in a session
    path = THREADS_DIR / f"{stamp}-{_slug(thread['name'])}-{tid}.md"
    path.write_text(md)
    _append_routed(thread, [t for t in thread["tabs"] if t["status"] == "routed"], idx)
    idx[this_id or tid] = {"thread_id": this_id or tid, "name": thread["name"], "path": str(path),
                           "parked_at": thread.get("parked_at")}
    (THREADS_DIR / INDEX_NAME).write_text(json.dumps(list(idx.values()), indent=1))

    sync = {"synced": False, "reason": "eve_sync not installed"}
    if _eve_sync_available():
        try:
            from eve_sync import sync_thread
            sync = sync_thread(thread, md)
        except Exception as e:  # never lose the local copy because sync failed
            sync = {"synced": False, "reason": f"{type(e).__name__}: {e}"}

    return {"path": str(path), "classifier": note, "statuses": statuses,
            "close_urls": _close_urls(thread), **sync}
