// Reports reading signals for this page to the background script.
// Nothing here leaves the browser; the background script decides what to keep.
(() => {
  let maxDepth = 0, highlights = 0, scrollBacks = 0, peakTop = 0, dirty = true;
  const highlightTexts = [];

  // A short description of the page from its own metadata. Journals and arXiv publish
  // citation_abstract / dc.description; most sites have og or meta descriptions.
  // Nothing is fetched and the page body isn't scraped beyond one paragraph.
  function pageAbstract() {
    const meta = (sel) => document.querySelector(sel)?.getAttribute("content")?.trim();
    let a =
      meta('meta[name="citation_abstract"]') ||
      meta('meta[name="dc.description"]') || meta('meta[name="DC.description"]') ||
      meta('meta[property="og:description"]') ||
      meta('meta[name="description"]') ||
      [...document.querySelectorAll("article p, main p, p")]
        .map((p) => p.innerText.trim())
        .find((t) => t.length >= 120);
    if (!a) return "";
    a = a.replace(/^abstract[:.\s]*/i, "").replace(/\s+/g, " ").trim();
    return a.length > 600 ? a.slice(0, 597) + "..." : a;
  }

  // Many sites scroll an inner container rather than the page, so measure whichever
  // element actually scrolled (capture phase sees scroll events from any element).
  addEventListener("scroll", (e) => {
    const el = e.target === document ? document.scrollingElement : e.target;
    if (!el || el.scrollHeight < el.clientHeight * 1.5) return; // not a main scroller
    const d = Math.min(1, (el.scrollTop + el.clientHeight) / el.scrollHeight);
    if (d > maxDepth) { maxDepth = d; dirty = true; }
    if (el.scrollTop > peakTop) peakTop = el.scrollTop;
    else if (el.scrollTop < peakTop - 400) { scrollBacks++; peakTop = el.scrollTop; dirty = true; } // re-reading
  }, { passive: true, capture: true });

  document.addEventListener("mouseup", () => {
    const text = String(getSelection()).replace(/\s+/g, " ").trim();
    if (text.length <= 20) return;
    highlights++;
    const clip = text.length > 300 ? text.slice(0, 297) + "..." : text;
    // skip a selection that extends or repeats one already saved
    const i = highlightTexts.findIndex((h) => clip.includes(h.replace(/\.\.\.$/, "")) || h.includes(clip));
    if (i >= 0) highlightTexts[i] = clip.length > highlightTexts[i].length ? clip : highlightTexts[i];
    else if (highlightTexts.length < 10) highlightTexts.push(clip);
    dirty = true;
  });

  function send(force) {
    if ((!dirty && !force) || !chrome.runtime?.id) return;
    dirty = false;
    try {
      chrome.runtime.sendMessage({
        type: "SIGNALS", scrollDepth: maxDepth, highlights, scrollBacks,
        highlightTexts, abstract,
      });
    } catch { /* extension reloaded; ignore */ }
  }

  // A page that fits on screen has been "scrolled" all the way by being visible.
  const se = document.scrollingElement;
  if (se && se.scrollHeight <= innerHeight * 1.1) maxDepth = 1;
  const abstract = pageAbstract();

  setInterval(() => send(false), 4000);
  // Resend periodically so a thread started after this page loaded still gets its signals.
  setInterval(() => send(true), 20000);
  document.addEventListener("visibilitychange", () => send(true));

  // The sidecar's /suggestions page has an "Open all" button. A page can't open many tabs
  // itself (popup blocking), so hand the URLs to the extension to open as one tab group.
  if (/^https?:\/\/(localhost|127\.0\.0\.1):8799\/suggestions/.test(location.href)) {
    document.addEventListener("click", (e) => {
      const b = e.target.closest && e.target.closest("#open-all");
      if (!b) return;
      e.preventDefault();
      let urls = [];
      try { urls = JSON.parse(b.dataset.urls || "[]"); } catch {}
      // if the page offers tick boxes (input[data-url]), open only the ticked sources
      const boxes = [...document.querySelectorAll("input[type=checkbox][data-url]")];
      if (boxes.length) urls = boxes.filter((x) => x.checked).map((x) => x.dataset.url);
      const name = b.dataset.name || document.title.replace(/^[^:]*:\s*/, "");
      chrome.runtime.sendMessage({ type: "OPEN_GROUP", urls, name });
    }, true);
  }
})();
