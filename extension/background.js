// eve-threads background service worker.
// Tracks the tabs in the active research thread, and parks the thread on demand:
// classify each tab, send it to the local sidecar (local markdown + optional Eve sync),
// then close the tabs.

const SIDECAR = "http://localhost:8799";
const SKIP = /^(chrome|chrome-extension|about|edge|devtools|view-source):|^https?:\/\/(localhost|127\.0\.0\.1):8799\//;
const GO = /^https?:\/\/(localhost|127\.0\.0\.1):8799\/go\/([\w-]+)/;
const KEEP_OPEN = /(^https?:\/\/(www\.)?claude\.ai)|localhost:8799/;

// ---------- state (persisted: the service worker can be stopped at any time) ----------

async function getState() {
  const { active = null, parked = [], focus = null } =
    await chrome.storage.local.get(["active", "parked", "focus"]);
  return { active, parked, focus };
}

// Serialize read-modify-write so concurrent events don't lose updates.
let queue = Promise.resolve();
function withState(fn) {
  const run = queue.then(async () => {
    const s = await getState();
    const patch = await fn(s);
    if (patch) await chrome.storage.local.set(patch);
  });
  queue = run.catch((e) => console.error("[eve-threads]", e));
  return run;
}

function newRecord(tab, openerUrl) {
  return {
    url: tab.url || tab.pendingUrl || "",
    title: tab.title || "",
    openerTabId: tab.openerTabId ?? null,
    openerUrl: openerUrl || null,
    firstSeen: Date.now(),
    focusMs: 0,
    scrollDepth: 0,
    highlights: 0,
    scrollBacks: 0,
    closedAt: null,
    later: false,
    abstract: "",
    highlightTexts: [],
  };
}

function closeFocus(s) {
  if (s.focus && s.active && s.active.tabs[s.focus.tabId]) {
    s.active.tabs[s.focus.tabId].focusMs += Date.now() - s.focus.since;
  }
}

// ---------- classification (heuristic; the sidecar may replace it with Laya) ----------

const PERSON_ORG_URL = [
  /scholar\.google\.[^/]+\/citations/i,
  /linkedin\.com\/(in|company)\//i,
  /^https?:\/\/(www\.)?(x|twitter)\.com\//i,
  /\/(people|faculty|team|staff|members|lab|about)(\/|$|\?)/i,
  /^https?:\/\/(www\.)?github\.com\/[^/]+\/?$/i,
];
const PERSON_ORG_TITLE = /\b(lab|laboratory|institute|professor|faculty|people|about us)\b/i;

function heuristicStatus(r) {
  if (r.later) return "unread"; // the user explicitly queued it
  if (PERSON_ORG_URL.some((re) => re.test(r.url)) || PERSON_ORG_TITLE.test(r.title)) return "person_org";
  const f = r.focusMs / 1000;
  const pdf = /\.pdf($|[?#])/i.test(r.url);
  if ((f >= 90 && (r.scrollDepth >= 0.6 || pdf)) || (r.highlights > 0 && f >= 45)) return "read";
  if (f >= 15 || r.scrollDepth >= 0.3) return "skimmed";
  return "unread";
}

function summarizeTabs(s, live = false) {
  if (!s.active) return [];
  return Object.entries(s.active.tabs)
    .filter(([, r]) => r.url && !SKIP.test(r.url))
    .map(([id, r]) => {
      let focusMs = r.focusMs;
      if (live && s.focus && +s.focus.tabId === +id) focusMs += Date.now() - s.focus.since;
      const rec = { ...r, focusMs };
      return {
        tab_id: +id,
        url: r.url,
        title: r.title || r.url,
        opener_url: r.openerUrl,
        status: heuristicStatus(rec),
        focus_s: Math.round(focusMs / 1000),
        scroll_depth: +r.scrollDepth.toFixed(2),
        highlights: r.highlights,
        scroll_backs: r.scrollBacks,
        seeded: !!r.seeded,
        later: !!r.later,
        abstract: r.abstract || "",
        highlight_texts: r.highlightTexts || [],
        closed: !!r.closedAt,
      };
    });
}

// ---------- thread lifecycle ----------

// rootTab is set when Claude started the thread: it's the localhost /go page, which isn't
// recorded itself, but tabs it opens are marked as suggested by Claude.
// Your Eve cortexes, for the popup's picker (refreshed at most once a minute).
let cortexCache = null, cortexAt = 0;
async function getCortexes() {
  if (cortexCache && Date.now() - cortexAt < 60000) return cortexCache;
  try {
    const r = await fetch(`${SIDECAR}/cortexes`, { signal: AbortSignal.timeout(2000) });
    if (!r.ok) return cortexCache;
    const j = await r.json();
    cortexCache = (j.cortexes || []).map((c) => (typeof c === "string" ? c : c.name)).filter(Boolean);
    cortexAt = Date.now();
  } catch {}
  return cortexCache;
}

// Several topics can be open at once in one session. Starting a thread while one is
// active adds a topic; at park, each tab is filed under its closest topic.
const topicNames = (a) => (a.topics || [{ name: a.name }]).map((t) => t.name);
const sessionName = (a) => topicNames(a).join(" + ");

async function startThread(name, rootTab = null, cortex = null) {
  const topic = { id: crypto.randomUUID().slice(0, 8), name: name || "Untitled research thread",
                  started_at: new Date().toISOString(), cortex: cortex || null }; // null = sidecar default (browsing)
  await withState(async (s) => {
    if (s.active) {
      s.active.topics = [...(s.active.topics || [{ id: "t0", name: s.active.name, started_at: s.active.startedAt }]), topic];
      s.active.name = sessionName(s.active);
      if (rootTab) s.active.goTabId = rootTab.id; // tabs Claude's start page opens count as suggested
      return { active: s.active };
    }
    const [cur] = rootTab ? [rootTab] : await chrome.tabs.query({ active: true, lastFocusedWindow: true });
    const active = {
      id: crypto.randomUUID(),
      name: topic.name,
      topics: [topic],
      startedAt: topic.started_at,
      tabs: {},
      lastTabId: null,
      focusOrder: [],
      goTabId: rootTab ? rootTab.id : null,
    };
    if (cur && !SKIP.test(cur.url || "")) {
      active.tabs[cur.id] = newRecord(cur, null);
      active.lastTabId = cur.id;
      active.focusOrder = [cur.id];
    }
    chrome.action.setBadgeText({ text: "●" });
    chrome.action.setBadgeBackgroundColor({ color: "#2f6f5e" });
    return { active, focus: cur ? { tabId: cur.id, since: Date.now() } : null };
  });
}

async function cancelThread() {
  await withState(async () => ({ active: null, focus: null }));
  chrome.action.setBadgeText({ text: "" });
}

let parking = false;

async function park() {
  if (parking) return { ok: false, error: "Already parking" };
  parking = true;
  try { return await parkOnce(); } finally { parking = false; }
}

async function parkOnce() {
  let payload = null;
  let openTabs = [];
  // Claim the thread immediately (active -> null) so a second trigger finds nothing to park.
  await withState(async (s) => {
    if (!s.active) return null;
    closeFocus(s);
    const tabs = summarizeTabs(s);
    const last = s.active.tabs[s.active.lastTabId];
    payload = {
      thread_id: s.active.id,
      name: sessionName(s.active),
      topics: s.active.topics || [{ id: "t0", name: s.active.name, started_at: s.active.startedAt }],
      started_at: s.active.startedAt,
      parked_at: new Date().toISOString(),
      last_focused_url: last ? last.url : null,
      // most recent last; the sidecar uses it to find the last on-topic tab
      focus_order: (s.active.focusOrder || []).map((id) => s.active.tabs[id]?.url).filter(Boolean),
      tabs: tabs.map(({ tab_id, closed, ...t }) => t),
    };
    openTabs = tabs.filter((t) => !t.closed).map((t) => ({ id: t.tab_id, url: t.url }));
    return { active: null, focus: null };
  });
  if (!payload) return { ok: false, error: "No active thread" };
  chrome.action.setBadgeText({ text: "…" });

  let result = { synced: false, classifier: "heuristic", path: null };
  try {
    const r = await fetch(`${SIDECAR}/park`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
      signal: AbortSignal.timeout(20000),
    });
    if (r.ok) result = await r.json();
    else result.error = `sidecar returned ${r.status}`;
  } catch {
    result.error = "sidecar offline: saved in the extension only";
  }
  if (result.statuses) {
    payload.tabs.forEach((t) => { if (result.statuses[t.url]) t.status = result.statuses[t.url]; });
  }

  // Close only tabs the sidecar is confident belong to this thread. Off-topic tabs, tabs
  // filed under another thread, and borderline ones stay open. Sidecar offline: close nothing.
  const closeSet = new Set(result.close_urls || []);
  const toClose = [];
  for (const { id, url } of openTabs) {
    if (!closeSet.has(url)) continue;
    try {
      const t = await chrome.tabs.get(id);
      if (!KEEP_OPEN.test(t.url || "")) toClose.push(id);
    } catch { /* already closed */ }
  }
  const all = await chrome.tabs.query({});
  if (toClose.length && toClose.length >= all.length) await chrome.tabs.create({});
  if (toClose.length) await chrome.tabs.remove(toClose);

  await withState(async (s) => ({
    parked: [{ ...payload, result }, ...s.parked.filter((p) => p.thread_id !== payload.thread_id)].slice(0, 30),
  }));
  chrome.action.setBadgeText({ text: "" });

  const NAMES = { read: "read", skimmed: "skimmed", unread: "unread", person_org: "people/orgs", routed: "filed under other threads", off_topic: "off-topic (kept open)" };
  const counts = Object.keys(NAMES)
    .map((k) => [k, payload.tabs.filter((t) => t.status === k).length])
    .filter(([, n]) => n)
    .map(([k, n]) => `${n} ${NAMES[k]}`)
    .join(" · ");
  chrome.notifications.create({
    type: "basic",
    iconUrl: "icon.png",
    title: `Parked "${payload.name}"`,
    message: `${payload.tabs.length} tabs: ${counts}\nClosed ${toClose.length}, left ${openTabs.length - toClose.length} open` +
      (result.synced ? "\nSaved locally + synced to Eve" : result.path ? "\nSaved locally" : `\n${result.error || ""}`),
  });
  return { ok: true, payload, result };
}

// Threads parked while the sidecar was down are kept "extension only".
// Resend them once it's back; the sidecar ignores repeats of the same thread.
let retrying = false;
async function retryUnsaved() {
  if (retrying) return;
  retrying = true;
  try {
    const { parked } = await getState();
    for (const p of parked.filter((x) => !x.result?.path)) {
      const { result: _old, ...payload } = p;
      try {
        const r = await fetch(`${SIDECAR}/park`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(payload),
          signal: AbortSignal.timeout(20000),
        });
        if (!r.ok) break;
        const res = await r.json();
        await withState(async (s) => ({
          parked: s.parked.map((x) => (x.thread_id === p.thread_id ? { ...x, result: res } : x)),
        }));
      } catch { break; }
    }
  } finally {
    retrying = false;
  }
}

// Collapse duplicates left by the earlier double-park bug (list is newest first).
withState(async (s) => {
  const seen = new Set();
  const parked = s.parked.filter((p) => !seen.has(p.thread_id) && seen.add(p.thread_id));
  return parked.length === s.parked.length ? null : { parked };
});

async function reopen(threadId, which) {
  const { parked } = await getState();
  const t = parked.find((p) => p.thread_id === threadId);
  if (!t) return { ok: false };
  const wanted = which === "all" ? null : new Set(["unread", "skimmed"]);
  const urls = t.tabs.filter((x) => !wanted || wanted.has(x.status)).map((x) => x.url);
  for (const url of urls) await chrome.tabs.create({ url, active: false });
  return { ok: true, opened: urls.length };
}

// ---------- browser events ----------

chrome.tabs.onCreated.addListener((tab) => {
  withState(async (s) => {
    if (!s.active) return null;
    const seeded = tab.openerTabId != null && tab.openerTabId === s.active.goTabId;
    let openerUrl = null;
    if (tab.openerTabId != null && !seeded) {
      openerUrl = s.active.tabs[tab.openerTabId]?.url || null;
      if (!openerUrl) { try { openerUrl = (await chrome.tabs.get(tab.openerTabId)).url; } catch {} }
    }
    s.active.tabs[tab.id] = { ...newRecord(tab, openerUrl), seeded };
    return { active: s.active };
  });
});

// Claude starts a thread by opening http://localhost:8799/go/<id> in Chrome.
// Fetch the topic and suggested URLs, start the thread, and open the suggestions.
const handledGo = new Set();
chrome.tabs.onUpdated.addListener((tabId, info, tab) => {
  const m = GO.exec(info.url || tab.url || "");
  if (m && !handledGo.has(m[2])) {
    handledGo.add(m[2]);
    startFromClaude(m[2], tab);
  }
});

async function startFromClaude(id, goTab) {
  let job;
  try {
    const r = await fetch(`${SIDECAR}/pending/${id}`);
    if (!r.ok) return;
    job = await r.json();
  } catch { return; }
  if (!job?.name) return;
  // A new topic from Claude joins the open session (see startThread); nothing is parked.
  await startThread(job.name, goTab, job.cortex || null);
  // Nothing is opened automatically: you read Claude's answer first, then open sources
  // yourself, one by one or with the "Open all" link Claude includes (see /open below).
}

// "Open all N sources" link in Claude's answer -> http://localhost:8799/open/<id>.
// Opens Claude's suggestions as one named tab group, then closes the link's own tab.
const OPEN = /^https?:\/\/(localhost|127\.0\.0\.1):8799\/open\/([\w-]+)/;
const handledOpen = new Set();
chrome.tabs.onUpdated.addListener((tabId, info, tab) => {
  const m = OPEN.exec(info.url || tab.url || "");
  if (m && !handledOpen.has(m[2])) {
    handledOpen.add(m[2]);
    openSuggestions(m[2], tab);
  }
});

async function openSuggestions(id, linkTab) {
  let job;
  try {
    const r = await fetch(`${SIDECAR}/pending/${id}`);
    if (!r.ok) return;
    job = await r.json();
  } catch { return; }
  const urls = job.suggested_urls || [];
  if (!urls.length) return;
  await openGroup(urls, job.name, linkTab.windowId);
  try { await chrome.tabs.remove(linkTab.id); } catch {}
}

// Open sources as one named tab group. Starts recording if no thread is running.
async function openGroup(urls, name, windowId) {
  urls = (urls || []).filter((u) => /^https?:\/\//.test(u)).slice(0, 20);
  if (!urls.length) return { ok: false };
  const { active } = await getState();
  if (!active) await startThread(name);
  const ids = [];
  for (let i = 0; i < urls.length; i++) {
    const t = await chrome.tabs.create({ url: urls[i], windowId, active: i === 0 });
    ids.push(t.id);
  }
  try {
    const groupId = await chrome.tabs.group({ tabIds: ids, createProperties: { windowId } });
    await chrome.tabGroups.update(groupId, { title: (name || "Sources").slice(0, 40), color: "green" });
  } catch { /* tab groups unavailable: tabs are still open */ }
  return { ok: true, opened: ids.length };
}

chrome.tabs.onUpdated.addListener((tabId, info, tab) => {
  if (!info.url && !info.title) return;
  withState(async (s) => {
    const r = s.active?.tabs[tabId];
    if (!r) return null;
    if (tab.url) r.url = tab.url;
    if (tab.title) r.title = tab.title;
    return { active: s.active };
  });
});

chrome.tabs.onRemoved.addListener((tabId) => {
  withState(async (s) => {
    const r = s.active?.tabs[tabId];
    if (!r) return null;
    if (s.focus && +s.focus.tabId === tabId) { closeFocus(s); s.focus = null; }
    r.closedAt = Date.now();
    return { active: s.active, focus: s.focus };
  });
});

chrome.tabs.onActivated.addListener(({ tabId }) => {
  withState(async (s) => {
    if (!s.active) return null;
    closeFocus(s);
    if (s.active.tabs[tabId]) {
      s.active.lastTabId = tabId;
      s.active.focusOrder = [...(s.active.focusOrder || []).filter((id) => id !== tabId), tabId];
    }
    return { active: s.active, focus: { tabId, since: Date.now() } };
  });
});

chrome.windows.onFocusChanged.addListener((winId) => {
  withState(async (s) => {
    if (!s.active) return null;
    closeFocus(s);
    if (winId === chrome.windows.WINDOW_ID_NONE) return { active: s.active, focus: null };
    const [cur] = await chrome.tabs.query({ active: true, windowId: winId });
    return { active: s.active, focus: cur ? { tabId: cur.id, since: Date.now() } : null };
  });
});

chrome.commands.onCommand.addListener((cmd) => {
  if (cmd === "park-thread") park();
  if (cmd === "read-later") toggleLater();
});

// Mark the current tab to read later (toggle). Only tabs in the active thread.
async function toggleLater() {
  const [tab] = await chrome.tabs.query({ active: true, lastFocusedWindow: true });
  if (!tab) return;
  let now = null;
  await withState(async (s) => {
    const r = s.active?.tabs[tab.id];
    if (!r) return null;
    r.later = !r.later;
    now = r.later;
    return { active: s.active };
  });
  if (now === null) {
    flashBadge("–", "#9aa39e"); // no active thread, or this tab isn't in it
  } else {
    flashBadge(now ? "L" : "×", now ? "#8a5a9e" : "#9aa39e");
  }
}

function flashBadge(text, color) {
  chrome.action.setBadgeText({ text });
  chrome.action.setBadgeBackgroundColor({ color });
  setTimeout(async () => {
    const { active } = await getState();
    chrome.action.setBadgeText({ text: active ? "●" : "" });
    chrome.action.setBadgeBackgroundColor({ color: "#2f6f5e" });
  }, 1500);
}

chrome.runtime.onMessage.addListener((msg, sender, reply) => {
  (async () => {
    switch (msg.type) {
      case "SIGNALS": {
        const id = sender.tab?.id;
        await withState(async (s) => {
          const r = s.active?.tabs[id];
          if (!r) return null;
          r.scrollDepth = Math.max(r.scrollDepth, msg.scrollDepth || 0);
          r.highlights = Math.max(r.highlights, msg.highlights || 0);
          r.scrollBacks = Math.max(r.scrollBacks, msg.scrollBacks || 0);
          if (msg.abstract) r.abstract = msg.abstract;
          if (Array.isArray(msg.highlightTexts) && msg.highlightTexts.length >= (r.highlightTexts || []).length) {
            r.highlightTexts = msg.highlightTexts.slice(0, 10);
          }
          return { active: s.active };
        });
        return reply({ ok: true });
      }
      case "GET_STATE": {
        const s = await getState();
        let sidecar = { ok: false };
        try {
          const r = await fetch(`${SIDECAR}/health`, { signal: AbortSignal.timeout(800) });
          if (r.ok) sidecar = { ok: true, ...(await r.json()) };
        } catch {}
        if (sidecar.ok) retryUnsaved(); // fire and forget
        const cortexes = sidecar.ok ? await getCortexes() : null;
        let active = null;
        if (s.active) {
          const tabs = summarizeTabs(s, true);
          const topics = topicNames(s.active);
          let groups = null; // live: which topic each tab belongs to (only with 2+ topics)
          if (sidecar.ok && topics.length > 1 && tabs.length) {
            try {
              const r = await fetch(`${SIDECAR}/route`, {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ topics, tabs: tabs.map(({ url, title, seeded, later, opener_url }) => ({ url, title, seeded, later, opener_url })) }),
                signal: AbortSignal.timeout(1500),
              });
              if (r.ok) groups = (await r.json()).groups;
            } catch {}
          }
          active = { name: s.active.name, topics, topicList: s.active.topics || [], startedAt: s.active.startedAt, tabs, groups };
        }
        return reply({ active, parked: s.parked, sidecar, cortexes });
      }
      case "START": await startThread(msg.name, null, msg.cortex || null); return reply({ ok: true });
      case "OPEN_GROUP": // "Open all" button on the sidecar's /suggestions page
        if (!/^https?:\/\/(localhost|127\.0\.0\.1):8799\//.test(sender.tab?.url || "")) return reply({ ok: false });
        return reply(await openGroup(msg.urls, msg.name, sender.tab.windowId));
      case "CANCEL": await cancelThread(); return reply({ ok: true });
      case "PARK": return reply(await park());
      case "REOPEN": return reply(await reopen(msg.threadId, msg.which));
      default: return reply({ ok: false });
    }
  })();
  return true; // async reply
});
