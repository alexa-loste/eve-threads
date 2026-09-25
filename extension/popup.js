const $ = (s) => document.querySelector(s);
const send = (msg) => chrome.runtime.sendMessage(msg);

const fmtDur = (s) => (s >= 60 ? `${Math.floor(s / 60)}m ${String(s % 60).padStart(2, "0")}s` : `${s}s`);
const fmtTime = (iso) => new Date(iso).toLocaleString([], { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
const LABEL = { read: "read", skimmed: "skimmed", unread: "unread", person_org: "person/org", off_topic: "off-topic", routed: "other thread" };

function el(tag, attrs = {}, ...kids) {
  const e = document.createElement(tag);
  Object.entries(attrs).forEach(([k, v]) => (k === "class" ? (e.className = v) : e.setAttribute(k, v)));
  kids.flat().forEach((k) => e.append(k));
  return e;
}

function tabRow(t) {
  const sig = [
    `focused ${fmtDur(t.focus_s)}`,
    `scrolled ${Math.round(t.scroll_depth * 100)}%`,
    t.highlights ? `${t.highlights} highlight${t.highlights > 1 ? "s" : ""}` : null,
    t.scroll_backs ? `re-read ${t.scroll_backs}×` : null,
    t.seeded ? "suggested by Claude" : null,
    t.later ? "marked for later" : null,
    t.closed ? "closed" : null,
  ].filter(Boolean).join(" · ");
  return el("li", {},
    el("div", { class: "row" },
      el("span", { class: `pill ${t.status}` }, LABEL[t.status]),
      el("span", { class: "title", title: t.url }, t.title)),
    el("div", { class: "signals" }, sig));
}

function parkedRow(p) {
  const n = (k) => p.tabs.filter((t) => t.status === k).length;
  const where = p.result?.synced ? "local + Eve" : p.result?.path ? "local file" : "extension only";
  const reopenUnread = el("button", { class: "ghost" }, `Reopen queue (${n("unread") + n("skimmed")})`);
  const reopenAll = el("button", { class: "ghost" }, "Reopen all");
  reopenUnread.onclick = () => send({ type: "REOPEN", threadId: p.thread_id, which: "queue" });
  reopenAll.onclick = () => send({ type: "REOPEN", threadId: p.thread_id, which: "all" });
  return el("li", {},
    el("div", { class: "name" }, p.name),
    el("div", { class: "meta" },
      `${fmtTime(p.parked_at)} · ${n("read")} read · ${n("skimmed")} skimmed · ${n("unread")} unread · ${n("person_org")} people/orgs · ${where}`),
    el("div", { class: "parked-actions" }, reopenUnread, reopenAll));
}

async function render() {
  const s = await send({ type: "GET_STATE" });

  $("#idle").hidden = !!s.active;
  $("#active").hidden = !s.active;
  // The topic box stays available while a thread is open: a second topic runs alongside.
  $("#idle").hidden = false;
  const input = $("#thread-name");
  if (document.activeElement !== input) {
    input.placeholder = s.active ? "Add another topic to this session" : "What are you researching?";
    $("#start-form button").textContent = s.active ? "Add topic" : "Start thread";
  }

  // cortex picker: your existing cortexes only, so a typo can't create a new one
  const sel = $("#cortex");
  const names = s.cortexes && s.cortexes.length ? s.cortexes : ["browsing"];
  if (sel.dataset.names !== names.join("|")) {
    const keep = sel.value || (names.includes("browsing") ? "browsing" : names[0]);
    sel.replaceChildren(...names.map((n) => el("option", { value: n }, n)));
    sel.value = names.includes(keep) ? keep : names[0];
    sel.dataset.names = names.join("|");
  }

  if (s.active) {
    $("#active-name").textContent = (s.active.topicList || []).length
      ? s.active.topicList.map((t) => `${t.name} → ${t.cortex || "browsing"}`).join("  ·  ")
      : s.active.name;
    const mins = Math.round((Date.now() - new Date(s.active.startedAt)) / 60000);
    const n = s.active.topics.length;
    $("#active-meta").textContent = `${s.active.tabs.length} tabs · ${n} topic${n > 1 ? "s" : ""} · started ${mins} min ago`;
    const ul = $("#tabs");
    if (!s.active.tabs.length) {
      ul.replaceChildren(el("li", { class: "empty" }, "Open some tabs; they'll show up here."));
    } else if (s.active.groups && n > 1) {
      // live grouping: each tab under the topic it will be filed under at park
      const rows = [];
      [...s.active.topics.map((t, i) => [i, t]), [-1, "Not matching a topic"]].forEach(([i, name]) => {
        const tabs = s.active.tabs.filter((t) => (s.active.groups[t.url] ?? -1) === i);
        if (!tabs.length) return;
        rows.push(el("li", { class: "group" }, name), ...tabs.map(tabRow));
      });
      ul.replaceChildren(...rows);
    } else {
      ul.replaceChildren(...s.active.tabs.map(tabRow));
    }
  }

  const parked = $("#parked");
  parked.replaceChildren(...(s.parked.length ? s.parked.map(parkedRow) : [el("li", { class: "empty" }, "Nothing parked yet.")]));

  const sc = s.sidecar;
  $("#sidecar").replaceChildren(
    el("span", { class: `dot ${sc.ok ? "on" : "off"}` }),
    sc.ok
      ? `Local sidecar running · off-topic filter: ${sc.topic_model === "ready" ? "on-device model" : sc.topic_model} · Eve sync: ${sc.eve_sync}`
      : "Local sidecar offline · threads will be kept in the extension only"
  );
}

$("#start-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  await send({ type: "START", name: $("#thread-name").value.trim(), cortex: $("#cortex").value || null });
  $("#thread-name").value = "";
  render();
});
$("#park").addEventListener("click", async () => {
  $("#park").textContent = "Parking…";
  await send({ type: "PARK" });
  render();
});
$("#cancel").addEventListener("click", async (e) => {
  e.preventDefault();
  await send({ type: "CANCEL" });
  render();
});

render();
setInterval(render, 2000);
