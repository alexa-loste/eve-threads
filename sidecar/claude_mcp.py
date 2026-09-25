"""Local MCP server so Claude (Desktop or Code) can start a research thread in the browser.

Tools:
  start_thread(name, suggested_urls)  start recording a thread; remember Claude's suggestions
  suggest_sources(urls)               add more suggestions to the current thread

Only a small "Recording research thread" page opens in Chrome, in the background; that
is how the extension learns a thread started. No source tabs are opened. The user reads Claude's answer and opens what
they choose. When they park, tabs that match Claude's suggestions are marked
"suggested by Claude", so Eve learns which suggestions they picked and read.

Register it with Claude Code (from the repo folder):
    claude mcp add eve-threads -- /ABS/PATH/eve-threads/sidecar/.venv/bin/python /ABS/PATH/eve-threads/sidecar/claude_mcp.py

Claude Desktop: add the same command under "mcpServers" in claude_desktop_config.json.
"""

from __future__ import annotations

import os
import subprocess

import httpx
from mcp.server.mcpserver import MCPServer

SIDECAR = os.getenv("EVE_THREADS_SIDECAR", "http://127.0.0.1:8799")

mcp = MCPServer("eve-threads")


def _down(e: Exception) -> str:
    return f"Could not reach the eve-threads sidecar at {SIDECAR} ({e}). Is run.sh running?"


@mcp.tool()
def start_thread(name: str, suggested_urls: list[str] | None = None,
                 cortex: str | None = None, reasons: list[str] | None = None,
                 answer: str | None = None) -> str:
    """Start recording a research thread in the user's browser.

    Call this when the user starts researching a question with you, and AGAIN with a
    new name whenever the conversation moves to a clearly different question: each
    direction becomes its own thread in their Eve memory (the current one is parked
    first). Use a short topic name, e.g. "Trump–Xi AI talks".

    `cortex`: which Eve cortex the thread and its sources are saved to. Ask the user at
    the start if it is not obvious (e.g. their existing project cortex on this topic;
    eve_spaces lists them). Leave empty for the default "browsing" cortex. It must be
    an existing cortex name; an unknown name falls back to the default.

    `reasons`: one short line per suggested URL, same order, saying why it is worth reading.
    `answer`: your answer to the user, in markdown. It is shown on the suggestions page
    above the sources, so they can read it and tick which ones to open.

    Put the sources you recommend in `suggested_urls`. They are NOT opened for the
    user: they read your answer and open what they choose. Anything they open is
    recorded, and suggestions they picked are marked as suggested by you.
    """
    try:
        r = httpx.post(f"{SIDECAR}/start",
                       json={"name": name, "suggested_urls": suggested_urls or [], "cortex": cortex,
                             "reasons": reasons or [], "answer": answer}, timeout=5)
        r.raise_for_status()
        data = r.json()
        go_url = data["go_url"]
    except (httpx.HTTPError, KeyError, ValueError) as e:
        return _down(e)
    # The extension starts the thread when this small page loads. -g keeps Chrome in the
    # background so the user stays in Claude; no source tabs are opened.
    opened = subprocess.run(["open", "-g", "-a", "Google Chrome", go_url], capture_output=True)
    if opened.returncode != 0:
        return f"Queued the thread, but could not reach Chrome. Open {go_url} in Chrome to start it."
    n = len(data.get("suggested_urls") or [])
    where = f' Saving to the "{data["cortex"]}" cortex.' if data.get("cortex") else ""
    return (f'Recording thread "{name}" in Chrome (a small start page opened in the background).{where}'
            + (f" {n} suggestion(s) noted. In your answer, list them as links and end with this"
               f" line: [Open all suggestions]({SIDECAR.replace('127.0.0.1', 'localhost')}/suggestions)."
               if n else "")
            + " They park it with Cmd+Shift+E when done.")


@mcp.tool()
def suggest_sources(urls: list[str], reasons: list[str] | None = None,
                    answer: str | None = None) -> str:
    """Add sources you are recommending to the CURRENT research thread.

    Call this whenever you recommend new papers or pages during the thread, and also
    show the user the links in your answer. `reasons`: one short line per URL, same order.
    `answer`: your answer in markdown, shown on the suggestions page above the sources. They are not opened for the user. If the
    new sources point to a clearly different question, call start_thread instead.
    """
    try:
        r = httpx.post(f"{SIDECAR}/suggest", json={"urls": urls, "reasons": reasons or [], "answer": answer}, timeout=5)
        if r.status_code == 409:
            return "No thread is recording yet. Call start_thread first."
        r.raise_for_status()
        data = r.json()
    except (httpx.HTTPError, ValueError) as e:
        return _down(e)
    return (f'Noted {len(data.get("added") or [])} suggestion(s) on thread "{data.get("thread")}".'
            f" List them as links in your answer and end with: [Open all suggestions]"
            f"({SIDECAR.replace('127.0.0.1', 'localhost')}/suggestions)")


if __name__ == "__main__":
    mcp.run()
