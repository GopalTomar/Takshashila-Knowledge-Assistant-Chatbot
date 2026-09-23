/* Takshashila Knowledge Assistant — static frontend (GitHub Pages).
 * Talks to the backend API configured at build time in config.js
 * (window.TK_CONFIG.apiBaseUrl). No secrets live here: an optional staff
 * access token is typed by the user and kept in sessionStorage only.
 * All untrusted text is escaped before rendering; links must be http(s).
 */
(function () {
  "use strict";

  var CFG = window.TK_CONFIG || {};
  var API = String(CFG.apiBaseUrl || "").replace(/\/+$/, "");
  var TOKEN_KEY = "tk_access_token";
  var EXAMPLES = [
    "What is Takshashila Institution?",
    "What are Takshashila's research areas?",
    "What has Takshashila published about geospatial technology?",
    "Tell me about the Geospatial Research programme",
  ];

  var $ = function (id) { return document.getElementById(id); };
  var form = $("ask-form"), q = $("q"), btn = $("ask-btn");
  var statusEl = $("status"), answerEl = $("answer"), bodyEl = $("answer-body");
  var sourcesEl = $("sources"), listEl = $("source-list");

  function getToken() { try { return sessionStorage.getItem(TOKEN_KEY) || ""; } catch (e) { return ""; } }
  function setToken(v) {
    try { v ? sessionStorage.setItem(TOKEN_KEY, v) : sessionStorage.removeItem(TOKEN_KEY); } catch (e) { /* private mode */ }
    renderScope();
  }

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }
  function safeUrl(u) { return /^https?:\/\//i.test(String(u || "")) ? String(u) : ""; }

  /* Minimal, escaped Markdown: **bold**, *italics*, bullet lists, paragraphs,
   * and [Source N] → superscript link to the matching source card. */
  function renderAnswer(md, citations) {
    var nums = {};
    (citations || []).forEach(function (c) { nums[c.n] = c; });
    var lines = esc(md || "").split(/\n/);
    var html = [], inList = false;
    function inline(t) {
      t = t.replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>").replace(/(^|\W)\*(\S.*?\S|\S)\*(?=\W|$)/g, "$1<em>$2</em>");
      return t.replace(/\[Source (\d+)\]/g, function (m, n) {
        return nums[n] ? '<a class="cite" href="#src-' + n + '" title="' + esc(nums[n].title) + '">' + n + "</a>" : "";
      });
    }
    lines.forEach(function (raw) {
      var line = raw.trim();
      var li = /^[-*•]\s+(.*)$/.exec(line);
      if (li) {
        if (!inList) { html.push("<ul>"); inList = true; }
        html.push("<li>" + inline(li[1]) + "</li>");
        return;
      }
      if (inList) { html.push("</ul>"); inList = false; }
      if (!line) return;
      var h = /^#{1,4}\s+(.*)$/.exec(line);
      html.push(h ? "<p><strong>" + inline(h[1]) + "</strong></p>" : "<p>" + inline(line) + "</p>");
    });
    if (inList) html.push("</ul>");
    return html.join("");
  }

  function showStatus(kind, text) {
    statusEl.hidden = false;
    statusEl.className = "status " + kind;
    statusEl.textContent = "";
    if (kind === "loading") {
      var sp = document.createElement("span"); sp.className = "spinner"; statusEl.appendChild(sp);
    }
    statusEl.appendChild(document.createTextNode(text));
  }

  function el(tag, cls, text) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text != null) e.textContent = text;
    return e;
  }

  function renderSources(citations) {
    listEl.textContent = "";
    (citations || []).forEach(function (c) {
      var li = el("li", "source"); li.id = "src-" + c.n;
      var top = el("div", "source-top");
      top.appendChild(el("span", "num", String(c.n)));
      if (c.content_type_label) top.appendChild(el("span", "chip", c.content_type_label));
      if (c.access === "internal") top.appendChild(el("span", "chip internal", "Internal"));
      li.appendChild(top);
      var h = el("h3"), url = safeUrl(c.url);
      if (url) {
        var a = el("a", null, c.title); a.href = url; a.target = "_blank"; a.rel = "noopener noreferrer";
        h.appendChild(a);
      } else { h.textContent = c.title; }
      li.appendChild(h);
      var bits = [];
      if (c.authors && c.authors.length) bits.push(c.authors.join(", "));
      if (c.publisher) bits.push(c.publisher);
      if (c.date) bits.push(c.date);
      bits.push(c.source_label || c.source);
      if (c.page_number) bits.push("p. " + c.page_number);
      li.appendChild(el("div", "source-meta", bits.filter(Boolean).join(" · ")));
      if (c.excerpt) li.appendChild(el("p", "excerpt", c.excerpt));
      if (url) {
        var open = el("a", "open-link", "Open source ↗"); open.href = url; open.target = "_blank"; open.rel = "noopener noreferrer";
        li.appendChild(open);
      }
      listEl.appendChild(li);
    });
    sourcesEl.hidden = !(citations && citations.length);
  }

  function renderResult(data) {
    statusEl.hidden = true;
    answerEl.hidden = false;
    bodyEl.innerHTML = renderAnswer(data.answer, data.citations);
    var conf = (data.confidence || "none").toLowerCase();
    var badge = $("confidence");
    badge.className = "badge " + conf;
    badge.textContent = conf === "none" ? (data.metadata && data.metadata.mode === "search" ? "Search" : "No evidence")
      : conf.charAt(0).toUpperCase() + conf.slice(1) + " confidence";
    var m = data.metadata || {};
    $("answer-meta").textContent = [
      m.latency_seconds != null ? "Answered in " + m.latency_seconds + "s" : "",
      m.scope === "internal" ? "Includes internal sources" : "Public sources",
      m.kb_version ? "KB " + m.kb_version : "",
    ].filter(Boolean).join(" · ");
    renderSources(data.citations);
  }

  function friendlyError(status, body) {
    var msg = body && body.message ? body.message : "";
    if (status === 429) return "Too many questions in a short time — please wait a minute and try again.";
    if (status === 401) return "Your access token was not accepted. Remove it or enter a valid token.";
    if (status === 503) return msg || "The assistant is starting up or temporarily unavailable. Please try again shortly.";
    if (status === 504) return "That took too long. Please try again, perhaps with a shorter answer length.";
    if (status === 422) return "Please enter a question (2–1000 characters).";
    return msg || "Something went wrong. Please try again.";
  }

  function ask(ev) {
    if (ev) ev.preventDefault();
    var query = q.value.trim();
    if (query.length < 2) { q.focus(); return; }
    if (!API) { showStatus("error", "The assistant's API address is not configured for this site."); return; }
    var mode = (form.querySelector("input[name=mode]:checked") || {}).value || "normal";
    btn.disabled = true;
    answerEl.hidden = true; sourcesEl.hidden = true;
    showStatus("loading", mode === "search" ? "Searching the knowledge base…" : "Searching sources and composing a verified answer…");
    var headers = { "Content-Type": "application/json" };
    var tok = getToken();
    if (tok) headers.Authorization = "Bearer " + tok;
    var ctrl = new AbortController();
    var timer = setTimeout(function () { ctrl.abort(); }, 90000);
    fetch(API + "/api/query", { method: "POST", headers: headers, signal: ctrl.signal,
      body: JSON.stringify({ query: query, mode: mode }) })
      .then(function (r) {
        return r.json().catch(function () { return {}; }).then(function (b) { return { ok: r.ok, status: r.status, body: b }; });
      })
      .then(function (res) {
        if (!res.ok) { showStatus("error", friendlyError(res.status, res.body)); return; }
        renderResult(res.body);
        answerEl.scrollIntoView({ behavior: "smooth", block: "start" });
      })
      .catch(function (e) {
        showStatus("error", e && e.name === "AbortError" ? "The request timed out. Please try again."
          : "Could not reach the assistant. Check your connection and try again.");
      })
      .finally(function () { clearTimeout(timer); btn.disabled = false; });
  }

  function renderScope() {
    $("scope-note").textContent = getToken()
      ? "Staff access on — answers may include the internal Commit knowledge base."
      : "Answers use Takshashila's public website. Staff can add an access token for internal sources.";
  }

  function checkHealth() {
    var s = $("api-state");
    if (!API) { s.textContent = "API not configured"; s.className = "api-state down"; return; }
    fetch(API + "/api/health").then(function (r) { return r.json(); }).then(function (h) {
      if (h.ready) { s.textContent = "Service online" + (h.kb && h.kb.version ? " · KB " + h.kb.version : ""); s.className = "api-state ok"; }
      else { s.textContent = "Service starting…"; s.className = "api-state warn"; setTimeout(checkHealth, 8000); }
    }).catch(function () { s.textContent = "Service unreachable"; s.className = "api-state down"; });
  }

  // Wire up
  form.addEventListener("submit", ask);
  q.addEventListener("keydown", function (e) { if (e.key === "Enter" && !e.shiftKey) { ask(e); } });
  var ex = $("examples");
  EXAMPLES.forEach(function (t) {
    var b = el("button", null, t); b.type = "button";
    b.addEventListener("click", function () { q.value = t; ask(); });
    ex.appendChild(b);
  });
  var dlg = $("access-dialog");
  $("access-btn").addEventListener("click", function () { $("token").value = getToken(); dlg.showModal(); });
  $("access-form").addEventListener("submit", function (e) {
    var action = e.submitter ? e.submitter.value : "save";
    setToken(action === "clear" ? "" : $("token").value.trim());
  });
  renderScope();
  checkHealth();

  // Shareable links: ?q=<question>[&mode=short|normal|detailed|search]
  try {
    var params = new URLSearchParams(window.location.search);
    var preset = (params.get("q") || "").slice(0, 1000);
    var pmode = params.get("mode");
    if (pmode) {
      var radio = form.querySelector('input[name=mode][value="' + pmode.replace(/[^a-z]/g, "") + '"]');
      if (radio) radio.checked = true;
    }
    if (preset) { q.value = preset; ask(); }
  } catch (e) { /* old browsers: ignore */ }
})();
