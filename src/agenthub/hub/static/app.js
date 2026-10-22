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
  // Chat window: the Chat button loads /ui/messenger into <section id="messenger">; a [data-chat] row
  // loads its conversation (/ui/messenger/<key>) there. The + button's menu opens New task / project /
  // agent forms in the panel (items new:<kind>). Only one of panel and chat window is open at a time.
  var panel = document.getElementById("panel");
  var scrim = document.getElementById("panel-scrim");
  var messenger = document.getElementById("messenger");
  var fabChat = document.getElementById("fab-chat");
  var fabPlus = document.getElementById("fab-plus");
  var plusMenu = document.getElementById("plus-menu");
  var opener = null, openerItem = null, fallback = null, chatFallback = null;
  // Whether a response for the panel or the chat window is still wanted: one that arrives after the
  // operator closed it (or opened the other) is dropped instead of opening it again.
  var wantPanel = false, wantMessenger = false;

  function panelOpen() { return panel.classList.contains("open"); }
  function messengerOpen() { return !messenger.hidden; }
  function menuOpen() { return !plusMenu.hidden; }

  function syncScrim() {
    var any = panelOpen() || messengerOpen();
    scrim.hidden = !any;
    document.body.classList.toggle("overlay-open", any);
  }

  function openPanel(item, href) {
    closeMenu(false);
    closeMessenger(false);
    fallback = href;
    wantPanel = true;
    var url = "/ui/panel?" + new URLSearchParams({ item: item, next: location.pathname });
    htmx.ajax("GET", url, { target: panel, swap: "innerHTML" });
  }

  function closePanel(restore) {
    wantPanel = false;
    if (!panelOpen()) return;
    panel.classList.remove("open");
    panel.setAttribute("inert", "");
    syncScrim();
    // The 10 s live refresh may have replaced the element that opened the panel; find its twin.
    var back = opener && opener.isConnected ? opener
      : openerItem && document.querySelector('main a[data-panel="' + CSS.escape(openerItem) + '"]');
    if (back && restore !== false) back.focus();
    opener = openerItem = null;
    // Empty it once it has slid out, so nothing inside (an approval thread) keeps polling.
    setTimeout(function () { if (!panelOpen()) panel.replaceChildren(); }, 400);
  }

  function loadMessenger(url, href) {
    chatFallback = href || "/ui/chats";
    wantMessenger = true;
    htmx.ajax("GET", url, { target: messenger, swap: "innerHTML" });
  }

  function openMessenger() {
    closeMenu(false);
    closePanel(false);
    loadMessenger("/ui/messenger");
  }

  function closeMessenger(restore) {
    wantMessenger = false;
    if (!messengerOpen()) return;
    messenger.hidden = true;
    messenger.replaceChildren();  // so its list and thread stop polling
    fabChat.setAttribute("aria-expanded", "false");
    syncScrim();
    if (restore !== false) fabChat.focus();
  }

  function openMenu() {
    plusMenu.hidden = false;
    fabPlus.setAttribute("aria-expanded", "true");
    plusMenu.querySelector("button").focus();
  }

  function closeMenu(restore) {
    if (!menuOpen()) return;
    plusMenu.hidden = true;
    fabPlus.setAttribute("aria-expanded", "false");
    if (restore) fabPlus.focus();
  }

  // The chat window's search box hides the rows (and empty sections) that don't match.
  function filterChats() {
    var box = messenger.querySelector("[data-chat-filter]");
    if (!box) return;
    var q = box.value.trim().toLowerCase();
    messenger.querySelectorAll(".clist > li").forEach(function (li) {
      li.hidden = !!q && li.textContent.toLowerCase().indexOf(q) < 0;
    });
    messenger.querySelectorAll(".clist").forEach(function (ul) {
      var any = !!ul.querySelector("li:not([hidden])");
      ul.hidden = !any;
      var head = ul.previousElementSibling;
      if (head && head.classList.contains("chat-group")) head.hidden = !any;
    });
  }

  // The reason an error page gives (its .error line), as plain text.
  function errorDetail(xhr) {
    try {
      var p = new DOMParser().parseFromString(xhr.responseText, "text/html").querySelector(".error");
      return p ? p.textContent.trim() : "";
    } catch (e) { return ""; }
  }

  if (panel && scrim && messenger && fabChat && fabPlus && plusMenu) {
    document.addEventListener("click", function (evt) {
      if (evt.button !== 0 || evt.metaKey || evt.ctrlKey || evt.shiftKey || evt.altKey) return;  // new tab: follow href
      var t = evt.target;
      if (t.closest("#fab-plus")) { if (menuOpen()) closeMenu(true); else openMenu(); return; }
      var make = t.closest("[data-new]");
      if (make) {
        openPanel("new:" + make.getAttribute("data-new"), null);
        opener = fabPlus;
        openerItem = null;
        return;
      }
      if (menuOpen() && !plusMenu.contains(t)) closeMenu(false);
      if (t.closest("#fab-chat")) {
        evt.preventDefault();
        if (messengerOpen()) closeMessenger(); else openMessenger();
        return;
      }
      if (t.closest("[data-messenger-close]")) { closeMessenger(); return; }
      if (t.closest("[data-chat-list]")) { evt.preventDefault(); loadMessenger("/ui/messenger"); return; }
      var row = t.closest("[data-chat]");
      if (row) {
        evt.preventDefault();
        loadMessenger("/ui/messenger/" + encodeURIComponent(row.getAttribute("data-chat")), row.href);
        return;
      }
      if (t === scrim) { closePanel(); closeMessenger(); return; }
      if (t.closest("[data-panel-close]")) { closePanel(); return; }
      var el = t.closest("[data-panel]");
      if (!el) return;
      var inner = t.closest("a, button, input, select, textarea, summary, label");
      if (inner && inner !== el && el.contains(inner)) return;  // e.g. a Message button on an agent card
      evt.preventDefault();
      var item = el.getAttribute("data-panel");
      if (messenger.contains(el)) {  // a conversation's header or question: the panel replaces the window
        opener = fabChat;
        openerItem = null;
      } else if (!panel.contains(el)) {  // opened from the page (a link inside the panel keeps the first opener)
        opener = el.matches("a") ? el : el.querySelector('a[data-panel="' + CSS.escape(item) + '"]') || el;
        openerItem = item;
      }
      var link = el.matches("a") ? el : el.querySelector("a[href]");
      openPanel(item, link && link.href);
    });

    // Escape closes the innermost thing: the + menu, then the panel, then the chat window.
    document.addEventListener("keydown", function (evt) {
      if (evt.key === "Escape") {
        if (menuOpen()) closeMenu(true);
        else if (panelOpen()) closePanel();
        else if (messengerOpen()) closeMessenger();
        else return;
        evt.preventDefault();
        return;
      }
      if (menuOpen() && plusMenu.contains(document.activeElement) && (evt.key === "ArrowDown" || evt.key === "ArrowUp")) {
        evt.preventDefault();
        var items = Array.prototype.slice.call(plusMenu.querySelectorAll("button"));
        var i = items.indexOf(document.activeElement) + (evt.key === "ArrowDown" ? 1 : -1);
        items[(i + items.length) % items.length].focus();
      }
    });

    // Tabbing out of the + menu closes it.
    plusMenu.addEventListener("focusout", function (evt) {
      if (evt.relatedTarget && !plusMenu.contains(evt.relatedTarget) && evt.relatedTarget !== fabPlus) closeMenu(false);
    });

    document.addEventListener("input", function (evt) {
      if (evt.target.matches && evt.target.matches("[data-chat-filter]")) filterChats();
    });

    function polledThread(el) {
      return el !== panel && el !== messenger && el.classList &&
        (panel.contains(el) && el.classList.contains("dthread") || messenger.contains(el) && el.classList.contains("mthread"));
    }

    // A polled thread keeps the reader's place unless they were at the newest message.
    document.addEventListener("htmx:beforeSwap", function (evt) {
      var th = evt.detail.target;
      if (th === panel && !wantPanel || th === messenger && !wantMessenger) { evt.detail.shouldSwap = false; return; }
      if (polledThread(th)) th.dataset.atEnd = String(th.scrollHeight - th.scrollTop - th.clientHeight < 8);
    });

    document.addEventListener("htmx:afterSwap", function (evt) {
      var th = evt.detail.target;
      var posted = evt.detail.requestConfig && evt.detail.requestConfig.verb === "post";
      if (polledThread(th)) {
        if (th.dataset.atEnd === "true") th.scrollTop = th.scrollHeight;
        return;
      }
      if (messenger.contains(th) && th.classList.contains("mscroll")) { filterChats(); return; }
      if (th === messenger) {
        messenger.hidden = false;
        fabChat.setAttribute("aria-expanded", "true");
        syncScrim();
        var thread = messenger.querySelector(".mthread");
        if (thread) thread.scrollTop = thread.scrollHeight;
        var go = (posted && messenger.querySelector("textarea")) || messenger.querySelector(".mhead .icon-btn");
        if (go) go.focus();
        return;
      }
      if (th !== panel) return;
      panel.removeAttribute("inert");
      panel.classList.add("open");
      syncScrim();
      if (!posted) panel.scrollTop = 0;
      panel.querySelectorAll(".dthread").forEach(function (th) { th.scrollTop = th.scrollHeight; });
      var focus = (posted && panel.querySelector("textarea")) || panel.querySelector("[data-panel-close]");
      if (focus) focus.focus();
    });

    document.addEventListener("htmx:responseError", function (evt) {
      var target = evt.detail.target;
      if (!(target === panel && wantPanel || target === messenger && wantMessenger)) return;
      if (evt.detail.requestConfig.verb === "get") {  // let the full page explain (404 and so on)
        var to = target === panel ? fallback : chatFallback;
        if (to) location.assign(to);
        return;
      }
      var form = evt.detail.elt;
      if (!form || !form.after) return;
      if (form.nextElementSibling && form.nextElementSibling.matches("p.error[role=alert]")) form.nextElementSibling.remove();
      var detail = errorDetail(evt.detail.xhr);
      var note = document.createElement("p");
      note.className = "error";
      note.setAttribute("role", "alert");
      note.textContent = "That didn't go through (error " + evt.detail.xhr.status + ")" + (detail ? ": " + detail : ".");
      form.after(note);
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
