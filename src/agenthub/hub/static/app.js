// agenthub dashboard: live feed over WebSocket and small HTMX niceties.
// No inline scripts or eval: the CSP only allows scripts from /ui/static.
(function () {
  "use strict";

  // Clear chat forms after a successful HTMX post.
  document.addEventListener("htmx:afterRequest", function (evt) {
    var form = evt.detail.elt;
    if (form && form.hasAttribute && form.hasAttribute("data-reset") && evt.detail.successful) form.reset();
  });

  // Slide-in panel: a [data-panel] link or card loads /ui/panel?item=... into <aside id="panel">
  // and slides it in from the left. Without JavaScript the same links go to the item's full page.
  var panel = document.getElementById("panel");
  var scrim = document.getElementById("panel-scrim");
  var opener = null, openerItem = null, fallback = null;

  function panelOpen() { return panel.classList.contains("open"); }

  function openPanel(item, href) {
    fallback = href;
    var url = "/ui/panel?" + new URLSearchParams({ item: item, next: location.pathname });
    htmx.ajax("GET", url, { target: panel, swap: "innerHTML" });
  }

  function closePanel() {
    if (!panelOpen()) return;
    panel.classList.remove("open");
    panel.setAttribute("inert", "");
    scrim.hidden = true;
    // The 10 s live refresh may have replaced the element that opened the panel; find its twin.
    var back = opener && opener.isConnected ? opener
      : openerItem && document.querySelector('main a[data-panel="' + CSS.escape(openerItem) + '"]');
    if (back) back.focus();
    opener = openerItem = null;
    // Empty it once it has slid out, so nothing inside (an approval thread) keeps polling.
    setTimeout(function () { if (!panelOpen()) panel.replaceChildren(); }, 400);
  }

  if (panel && scrim) {
    document.addEventListener("click", function (evt) {
      if (evt.button !== 0 || evt.metaKey || evt.ctrlKey || evt.shiftKey || evt.altKey) return;  // new tab: follow href
      var t = evt.target;
      if (t === scrim || t.closest("[data-panel-close]")) { closePanel(); return; }
      var el = t.closest("[data-panel]");
      if (!el) return;
      var inner = t.closest("a, button, input, select, textarea, summary, label");
      if (inner && inner !== el && el.contains(inner)) return;  // e.g. a Message button on an agent card
      evt.preventDefault();
      var item = el.getAttribute("data-panel");
      if (!panel.contains(el)) {  // opened from the page (a link inside the panel keeps the first opener)
        opener = el.matches("a") ? el : el.querySelector('a[data-panel="' + CSS.escape(item) + '"]') || el;
        openerItem = item;
      }
      var link = el.matches("a") ? el : el.querySelector("a[href]");
      openPanel(item, link && link.href);
    });

    document.addEventListener("keydown", function (evt) {
      if (evt.key === "Escape" && panelOpen()) { evt.preventDefault(); closePanel(); }
    });

    // A polled thread inside the panel keeps the reader's place unless they were at the newest message.
    document.addEventListener("htmx:beforeSwap", function (evt) {
      var th = evt.detail.target;
      if (th !== panel && panel.contains(th) && th.classList.contains("dthread")) {
        th.dataset.atEnd = String(th.scrollHeight - th.scrollTop - th.clientHeight < 8);
      }
    });

    document.addEventListener("htmx:afterSwap", function (evt) {
      var th = evt.detail.target;
      if (th !== panel && panel.contains(th) && th.classList.contains("dthread")) {
        if (th.dataset.atEnd === "true") th.scrollTop = th.scrollHeight;
        return;
      }
      if (th !== panel) return;
      var posted = evt.detail.requestConfig && evt.detail.requestConfig.verb === "post";
      panel.removeAttribute("inert");
      panel.classList.add("open");
      scrim.hidden = false;
      if (!posted) panel.scrollTop = 0;
      panel.querySelectorAll(".dthread").forEach(function (th) { th.scrollTop = th.scrollHeight; });
      var focus = (posted && panel.querySelector("textarea")) || panel.querySelector("[data-panel-close]");
      if (focus) focus.focus();
    });

    document.addEventListener("htmx:responseError", function (evt) {
      if (evt.detail.target !== panel) return;
      if (evt.detail.requestConfig.verb === "get") {  // let the full page explain (404 and so on)
        if (fallback) location.assign(fallback);
        return;
      }
      var note = document.createElement("p");
      note.className = "error";
      note.setAttribute("role", "alert");
      note.textContent = "That didn't go through (error " + evt.detail.xhr.status + "). Open the full page to see why.";
      if (evt.detail.elt && evt.detail.elt.after) evt.detail.elt.after(note);
    });
  }

  var feed = document.getElementById("feed");
  if (!feed) return;
  var live = document.getElementById("live");
  var filters = { project: feed.dataset.project, agent: feed.dataset.agent, type: feed.dataset.type };
  var delay = 1000;

  function setState(state) {
    if (live) { live.dataset.state = state; live.textContent = state; }
  }

  function matches(msg) {
    if (filters.project && msg.project !== filters.project) return false;
    if (filters.agent && msg.actor !== filters.agent) return false;
    if (filters.type && msg.event_type.indexOf(filters.type) !== 0) return false;
    return true;
  }

  function connect() {
    var scheme = location.protocol === "https:" ? "wss://" : "ws://";
    var ws = new WebSocket(scheme + location.host + "/ui/ws");
    var ping;
    ws.onopen = function () {
      setState("live");
      delay = 1000;
      ping = setInterval(function () { ws.send("ping"); }, 25000);
    };
    ws.onmessage = function (evt) {
      var msg;
      try { msg = JSON.parse(evt.data); } catch (e) { return; }
      if (msg.type !== "event" || !matches(msg)) return;
      // msg.html is rendered and escaped by the server's templates.
      feed.insertAdjacentHTML("afterbegin", msg.html);
      if (feed.firstElementChild) feed.firstElementChild.classList.add("new");
      while (feed.children.length > 300) feed.removeChild(feed.lastElementChild);
    };
    ws.onclose = function () {
      clearInterval(ping);
      setState("offline");
      setTimeout(connect, delay);
      delay = Math.min(delay * 2, 30000);
    };
  }
  connect();
})();
