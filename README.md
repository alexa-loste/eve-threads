# eve-threads

**Own your context window.**

Your research context shouldn't be locked inside whichever AI company makes your browser or your chatbot. eve-threads is a Chrome extension that follows a research thread across your tabs. When you step away, one keystroke **parks** it: every tab is classified (read, skimmed, unread, people/orgs), off-topic tabs are left out, a markdown file is written on your machine, and a summary is optionally synced to your own [Eve](https://evecortex.com) memory. Later, ask any AI connected to your memory "where was I on X?": Claude, ChatGPT, or anything that speaks MCP.

Built at Imbue's Punk Software Hack Night, Sept 24 2026. `#punksoftware`

## How it works

1. **Start a thread.** Either from the popup, or by asking Claude ("start a research thread on the Trump–Xi AI talks"). Claude calls a local `start_thread` tool and recording starts; nothing opens for you. You read Claude's answer and click the sources you want. The ones Claude suggested are recognized and marked "suggested by Claude", so later you can ask which of its suggestions you actually read.
2. **Run several topics at once if you like.** Start another thread while one is open (from the popup, or Claude calls `start_thread` again when your conversation moves to a new question) and it's added as a second topic. The popup groups your tabs under their topics live, and parking files each tab under its closest topic: one thread per topic.
3. **Research normally.** The extension records the tab tree (which tab opened which), how long each tab was focused, scroll depth, highlights, and scrolling back to re-read. The popup shows everything it knows about each tab.
4. **Park with ⌘⇧E** (Ctrl+Shift+E on Windows/Linux). The local sidecar:
   - marks each tab read / skimmed / unread from reading signals, and spots people and labs from URL patterns;
   - closes only tabs that clearly belong to a thread; off-topic and borderline tabs stay open, and mail/chat apps are never recorded;
   - compares each tab's title with the thread topic using a small on-device embedding model (all-MiniLM-L6-v2, 90MB) and leaves out tabs that aren't about it, such as a hotel page or your email;
   - files a tab under another thread you've parked when it clearly belongs there (a Trump–Xi story opened mid-way through AI-voting research lands in your Trump–Xi thread);
   - writes `threads/<date>-<topic>.md`, a plain file you can read and edit;
   - syncs the thread to your Eve memory if you've turned that on: one thread memory, plus a card for each source with its link. Pages you read closely are marked as things you already know.
5. **Pick up later.** Ask your AI "where was I on the Trump–Xi talks?", or reopen the unread queue from the popup.

What this adds beyond browser history: **attention** (what you actually read vs. opened and abandoned), **intent** (tabs grouped under the question you were pursuing, with the unread queue as your next step), and a **closed loop** (Claude suggests sources, and learns which ones you read).

## Honest software

- **Private:** reading signals never leave your machine. Classification runs locally. Only the parked summary is synced, and only if you turn sync on. The sidecar only answers the extension and local tools, never web pages.
- **Transparent:** the popup shows exactly what's tracked per tab, and the markdown lists anything that was left out and why.
- **Malleable:** threads are plain markdown files.
- **Portable:** synced threads are reachable from any MCP client, not one vendor's assistant.
- **Loyal:** there's no account, no analytics, and nothing here is trying to keep you browsing.

## Setup

Requires Chrome, Python 3.12, and [uv](https://docs.astral.sh/uv/).

```sh
cd sidecar
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -r requirements.txt
./run.sh            # starts the sidecar on localhost:8799 (first run downloads the 90MB topic model)
```

On macOS you can skip the terminal from now on: `./sidecar/install_autostart.sh` starts the sidecar at login and restarts it if it stops (`--uninstall` removes it).

Then in Chrome: `chrome://extensions` → turn on Developer mode → **Load unpacked** → choose the `extension/` folder. Check or change the shortcut at `chrome://extensions/shortcuts`.

Without the sidecar the extension still works; parked threads are kept in the extension only and are sent to the sidecar once it's back.

### Optional: let Claude start threads

Register the local MCP tool with Claude Code:

```sh
claude mcp add eve-threads -- /path/to/eve-threads/sidecar/.venv/bin/python /path/to/eve-threads/sidecar/claude_mcp.py
```

(Claude Desktop: add the same command to its MCP config.) The tool opens Chrome, so it needs to run on the same machine as your browser.

### Optional: sync parked threads to Eve

1. `cp sidecar/.env.example sidecar/.env` (leave `EVE_MCP_URL` empty to stay local-only)
2. `cd sidecar && .venv/bin/python -m eve_sync login` opens your browser once to sign in to Eve. The token is cached in `sidecar/.eve_token.json`, which is gitignored.

Parked threads are then written to your "browsing" cortex, and "where was I on X?" in Claude, ChatGPT, or any MCP client with Eve connected recalls them.

## Limitations

- **Topic filtering is title-based and deliberately loose.** The cutoff was set from a small hand-labeled set (14 page titles, 2 topics): clear junk scored ≤ 0.10, the least similar on-topic pages ~0.20, so the cutoff is 0.15. Borderline pages stay in the thread rather than being hidden. A page on a neighboring topic (a news story about AI policy in an AI-bias thread) can still slip through.
- **We tried an LLM-style decision model first.** [Laya](https://huggingface.co/convaiinnovations/laya) wasn't confident enough to classify read/skimmed/unread, and its relevance score tracked "research page vs. shopping page", not the topic. Embeddings did the job; Laya was dropped.
- Chrome's built-in PDF viewer doesn't allow content scripts, so PDF tabs are judged by focus time only.
- Tabs opened from desktop apps have no opener tab, so they join the active thread by time and rely on the topic filter.
- Reading signals (focus time, scroll, highlights) are a proxy for reading, not proof of it.
