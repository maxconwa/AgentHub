// agenthub dashboard: live feed over WebSocket and small HTMX niceties.
// No inline scripts or eval: the CSP only allows scripts from /ui/static.
(function () {
  "use strict";

  // app.css folds long chat bubbles only under html.js, so without this script every bubble shows whole.
  document.documentElement.classList.add("js");

  // Math: markdown.render leaves $…$ and $$…$$ as .math elements holding the TeX as text; typeset them
  // with the vendored KaTeX, on load and in everything HTMX swaps in (live regions, the panel, the chat
  // window). katex.render builds DOM nodes and sets their styles through the CSSOM, which the CSP
  // (style-src 'self') allows; KaTeX's HTML-string output carries inline style="" attributes it would block.
  // Invalid TeX throws and is shown as its source here, since KaTeX's own error display sets an inline
  // style. app.css clips .md and .agent-text, so \kern, \smash or \rlap can't paint outside them.
  function typeset(root) {
    if (!window.katex || !root || !root.querySelectorAll) return;
    root.querySelectorAll(".math:not([data-typeset])").forEach(function (el) {
      el.setAttribute("data-typeset", "");
      var tex = el.textContent;
      try {
        katex.render(tex, el, {
          displayMode: el.classList.contains("display"),
          throwOnError: true, trust: false, strict: "ignore", maxExpand: 1000, maxSize: 10,
        });
      } catch (e) {
        el.textContent = tex;
        el.classList.add("katex-error");
        el.title = e && e.message ? String(e.message) : "Invalid TeX";
      }
    });
  }
  if (window.htmx) htmx.onLoad(typeset); else typeset(document.body);

  // Live regions are replaced every 10 s. A <details data-remember> in one keeps the open or closed state the
  // operator last saw (keyed by its data-remember value) instead of snapping back to the server's default.
  var remembered = {};
  document.addEventListener("htmx:beforeSwap", function (evt) {
    var t = evt.detail.target;
    if (!t || !t.querySelectorAll) return;
    t.querySelectorAll("details[data-remember]").forEach(function (d) { remembered[d.getAttribute("data-remember")] = d.open; });
  });
  function restoreDetails(root) {
    if (!root || !root.querySelectorAll) return;
    root.querySelectorAll("details[data-remember]").forEach(function (d) {
      var key = d.getAttribute("data-remember");
      if (Object.prototype.hasOwnProperty.call(remembered, key)) d.open = remembered[key];
    });
  }
  if (window.htmx) htmx.onLoad(restoreDetails);

  // A hidden tab skips its live-region polls (the device meters and the rest); the next one after it shows again
  // catches up, so coming back to the tab redraws at once.
  document.addEventListener("htmx:beforeRequest", function (evt) {
    if (document.hidden && evt.detail.elt && evt.detail.elt.id === "live") evt.preventDefault();
  });
  document.addEventListener("visibilitychange", function () {
    var live = document.getElementById("live");
    if (!document.hidden && live && window.htmx) htmx.ajax("GET", live.getAttribute("hx-get"), {source: live});
  });

  // The device page's broadcast box empties once the hub has taken the message (a refusal keeps the text).
  document.addEventListener("htmx:afterRequest", function (evt) {
    var form = evt.detail.elt;
    if (!form || !form.matches || !form.matches(".broadcast form") || !evt.detail.successful) return;
    if (form.parentNode.querySelector("#broadcast-result .ok-line")) { form.reset(); forgetDrafts(form); }
  });

  // The Tasks page's bulk move: "All shown" ticks every row and a row unticked clears it again (half-ticked while
  // some are). The rows' boxes keep their ticks across the live refresh (hx-preserve); a row that arrives while
  // "All shown" is ticked comes in ticked, so what moves is what is ticked on screen. Done and cancelled are final,
  // so the buttons ask first, with the count.
  function picks() { return Array.prototype.slice.call(document.querySelectorAll("input[data-pick]")); }
  function syncPickAll() {
    var all = document.querySelector("input[data-pick-all]");
    if (!all) return;
    var boxes = picks(), ticked = boxes.filter(function (b) { return b.checked; }).length;
    all.checked = boxes.length > 0 && ticked === boxes.length;
    all.indeterminate = ticked > 0 && ticked < boxes.length;
  }
  document.addEventListener("change", function (evt) {
    var el = evt.target;
    if (el.matches && el.matches("input[data-pick-all]")) {
      picks().forEach(function (b) { b.checked = el.checked; });
      el.indeterminate = false;
    } else if (el.matches && el.matches("input[data-pick]")) {
      syncPickAll();
    }
  });
  if (window.htmx) htmx.onLoad(function (root) {
    if (!root || !root.querySelector || !root.querySelector("input[data-pick]")) return;
    var all = document.querySelector("input[data-pick-all]");
    if (all && all.checked) picks().forEach(function (b) { b.checked = true; });
    syncPickAll();
  });
  document.addEventListener("submit", function (evt) {
    var form = evt.target;
    if (!form || form.id !== "bulk-move") return;
    var n = picks().filter(function (b) { return b.checked; }).length;
    var done = evt.submitter && evt.submitter.value === "done";
    if (!n) { evt.preventDefault(); window.alert("Tick the tasks to move first."); return; }
    var ask = (done ? "Mark " : "Cancel ") + n + " task" + (n === 1 ? "" : "s") + (done ? " done" : "") +
      "? This can't be undone; a task whose state doesn't allow it is skipped.";
    if (!window.confirm(ask)) evt.preventDefault();
  });

  // Failed: an item that can fail carries data-fail-key (and data-failed while failed; see fail_attrs in
  // _macros.html). When a swap brings in a key failed that this page last showed not failed, the new element plays
  // .failed-new once. A key first seen already failed (on page load, say) only shows its red edge.
  var failedSeen = {};
  function playFailed(root) {
    if (!root || !root.querySelectorAll) return;
    var els = Array.prototype.slice.call(root.querySelectorAll("[data-fail-key]"));
    if (root.hasAttribute && root.hasAttribute("data-fail-key")) els.unshift(root);
    els.forEach(function (el) {
      if (!el.hasAttribute("data-failed") || failedSeen[el.getAttribute("data-fail-key")] !== false) return;
      el.classList.add("failed-new");
      el.addEventListener("animationend", function done(e) {
        if (e.target !== el) return;  // a child's animation bubbling up
        el.classList.remove("failed-new");
        el.removeEventListener("animationend", done);
      });
    });
    els.forEach(function (el) { failedSeen[el.getAttribute("data-fail-key")] = el.hasAttribute("data-failed"); });
  }
  if (window.htmx) htmx.onLoad(playFailed); else playFailed(document.body);

  // Clear chat forms after a successful HTMX post.
  document.addEventListener("htmx:afterRequest", function (evt) {
    var form = evt.detail.elt;
    if (form && form.hasAttribute && form.hasAttribute("data-reset") && evt.detail.successful) form.reset();
  });

  // Slide-in panel: a [data-panel] link or card loads /ui/panel?item=... into <aside id="panel">
  // and slides it in from the left. Without JavaScript the same links go to the item's full page. An agent's,
  // task's or project's panel is its chat.
  // Chat window: the Chat button loads the chat list (/ui/messenger) into <section id="messenger">; a row there
  // opens that chat in the panel. The + button's menu opens New task / project / agent forms in the panel
  // (items new:<kind>). Only one of panel and chat window is open at a time.
  var panel = document.getElementById("panel");
  var scrim = document.getElementById("panel-scrim");
  var messenger = document.getElementById("messenger");
  var fabChat = document.getElementById("fab-chat");
  var fabPlus = document.getElementById("fab-plus");
  var plusMenu = document.getElementById("plus-menu");
  var opener = null, openerItem = null, fallback = null;
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

  function openMessenger() {
    closeMenu(false);
    closePanel(false);
    wantMessenger = true;
    htmx.ajax("GET", "/ui/messenger", { target: messenger, swap: "innerHTML" });
  }

  function closeMessenger(restore) {
    wantMessenger = false;
    if (!messengerOpen()) return;
    messenger.hidden = true;
    messenger.replaceChildren();  // so its list stops polling
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
      if (t === scrim) { closePanel(); closeMessenger(); return; }
      if (t.closest("[data-panel-close]")) { closePanel(); return; }
      var el = t.closest("[data-panel]");
      if (!el) return;
      var inner = t.closest("a, button, input, select, textarea, summary, label");
      if (inner && inner !== el && el.contains(inner)) return;  // e.g. a Message button on an agent card
      evt.preventDefault();
      var item = el.getAttribute("data-panel");
      if (messenger.contains(el)) {  // a row of the chat list: the panel replaces the window
        opener = fabChat;
        openerItem = null;
      } else if (!panel.contains(el)) {  // opened from the page (a link inside the panel keeps the first opener)
        opener = el.matches("a") ? el : el.querySelector('a[data-panel="' + CSS.escape(item) + '"]') || el;
        openerItem = item;
      }
      var link = el.matches("a") ? el : el.querySelector("a[href]");
      openPanel(item, link && link.href);
    });

    // Escape closes the innermost thing: the + menu, then the panel, then the chat window. In a textarea
    // holding a half-typed message it only leaves the textarea, so the text isn't thrown away.
    document.addEventListener("keydown", function (evt) {
      if (evt.key === "Escape") {
        var typing = document.activeElement;
        if (typing && typing.tagName === "TEXTAREA" && typing.value.trim()) typing.blur();
        else if (menuOpen()) closeMenu(true);
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
      return el !== panel && el.classList && panel.contains(el) && el.classList.contains("dthread");
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
        var go = messenger.querySelector(".mhead .icon-btn");
        if (go) go.focus();
        return;
      }
      if (th !== panel) return;
      panel.removeAttribute("inert");
      panel.classList.add("open");
      syncScrim();
      if (!posted) panel.scrollTop = 0;
      panel.querySelectorAll(".dthread").forEach(function (th) { th.scrollTop = th.scrollHeight; });
      panel.querySelectorAll(".chat").forEach(showLatest);
      var focus = (posted && panel.querySelector("textarea")) || panel.querySelector("[data-panel-close]");
      if (focus) focus.focus();
    });

    document.addEventListener("htmx:responseError", function (evt) {
      var target = evt.detail.target;
      if (!(target === panel && wantPanel || target === messenger && wantMessenger)) return;
      if (evt.detail.requestConfig.verb === "get") {  // let the full page explain (404 and so on)
        var to = target === panel ? fallback : "/ui/chats";
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

  // Chats (_chat.html): a .chat[data-chat="<kind>:<ref>"] holds an <ol class="chat-log"> of rows, each an
  // li#cm-<id> with data-v (its version). The operator socket says, by id only, which row is new or changed
  // (chat.message, chat.updated) and which approval or question was decided; this fetches that row's HTML
  // (rendered and escaped by the server) and appends it, or swaps it in place: a "ran N tools" fold growing,
  // a card turning approved. A reader at the newest row stays there; one scrolled up keeps their place.
  // While the socket is down, every open chat catches up every 10 s instead.
  var NEAR_END = 40;  // px from the bottom that still counts as "at the newest"

  function logOf(chat) { return chat.querySelector(".chat-log"); }
  function atEnd(log) { return log.scrollHeight - log.scrollTop - log.clientHeight < NEAR_END; }
  function showLatest(chat) { var log = logOf(chat); if (log) log.scrollTop = log.scrollHeight; }
  function lastId(chat) {
    var rows = chat.querySelectorAll(".chat-log > li[data-msg]");
    return rows.length ? +rows[rows.length - 1].getAttribute("data-msg") : 0;
  }
  function openChats(key) {
    var all = Array.prototype.slice.call(document.querySelectorAll(".chat[data-chat]"));
    return key ? all.filter(function (c) { return c.getAttribute("data-chat") === key; }) : all;
  }

  // Rows from the server (HTML of <li> rows) into one chat: new ids in id order, known ids replaced when their
  // version changed. `live` marks rows that arrived while the operator watched: a failed line plays its hit.
  // Returns how many rows belong here (a question card's reply lands in the asker's own chat, maybe not this one).
  function upsertRows(chat, html, live) {
    var log = logOf(chat), n = 0;
    if (!log || !html) return 0;
    var follow = atEnd(log);
    var tpl = document.createElement("template");
    tpl.innerHTML = html;
    Array.prototype.slice.call(tpl.content.querySelectorAll("li[data-msg]")).forEach(function (li) {
      if (li.getAttribute("data-in") !== chat.getAttribute("data-chat")) return;
      n++;
      var id = +li.getAttribute("data-msg");
      var old = log.querySelector("#cm-" + id);
      if (old) {
        if (old.getAttribute("data-v") === li.getAttribute("data-v")) return;
        // Keep what the reader had open (a tools fold) and a half-typed card reply.
        old.querySelectorAll("details[data-remember]").forEach(function (d) {
          var twin = li.querySelector('details[data-remember="' + CSS.escape(d.getAttribute("data-remember")) + '"]');
          if (twin) twin.open = d.open;
        });
        old.querySelectorAll(".md.clamp.is-open").forEach(function (md) { setFolded(li.querySelector(".md.clamp"), false); });
        old.querySelectorAll("textarea").forEach(function (ta) {
          var twin = ta.name && ta.form && li.querySelector('form[action="' + CSS.escape(ta.form.getAttribute("action")) + '"] textarea[name="' + CSS.escape(ta.name) + '"]');
          if (twin && ta.value) twin.value = ta.value;
        });
        old.replaceWith(li);
      } else {
        var after = null;
        log.querySelectorAll("li[data-msg]").forEach(function (r) { if (!after && +r.getAttribute("data-msg") > id) after = r; });
        log.insertBefore(li, after);
      }
      if (live) {
        li.querySelectorAll("[data-failed]").forEach(function (el) { el.classList.add("failed-new"); });
        li.classList.add("cm-new");
      }
      if (window.htmx) htmx.process(li);
      typeset(li);
    });
    var empty = chat.querySelector(".chat-empty");
    if (empty && log.querySelector("li[data-msg]")) empty.remove();
    if (follow) log.scrollTop = log.scrollHeight;
    return n;
  }

  // A long bubble (_chat.html's bubble_body: .md.clamp, then its .show-more button) opens and folds again.
  function setFolded(md, folded) {
    if (!md) return;
    md.classList.toggle("is-open", !folded);
    var button = md.nextElementSibling;
    if (button && button.classList.contains("show-more")) {
      button.setAttribute("aria-expanded", String(!folded));
      button.textContent = folded ? "Show more" : "Show less";
    }
  }
  document.addEventListener("click", function (evt) {
    var button = evt.target.closest && evt.target.closest(".show-more");
    if (!button) return;
    var md = button.previousElementSibling;
    if (!md || !md.classList.contains("clamp")) return;
    var folding = md.classList.contains("is-open");
    setFolded(md, folding);
    if (folding && md.getBoundingClientRect().top < 0) md.scrollIntoView({ block: "nearest" });
  });

  function getRows(url) {
    return fetch(url, { credentials: "same-origin", headers: { "X-Chat": "1" } })
      .then(function (r) { return r.ok ? r.text() : ""; })
      .catch(function () { return ""; });
  }
  function refreshRow(chat, id) {
    getRows("/ui/chat/msg/" + encodeURIComponent(id)).then(function (html) { upsertRows(chat, html, true); });
  }
  function catchUp(chat) {
    getRows(chat.getAttribute("data-url") + "?after=" + lastId(chat)).then(function (html) { upsertRows(chat, html, true); });
  }
  function refreshCards(selector) {
    document.querySelectorAll(selector).forEach(function (card) {
      var li = card.closest("li[data-msg]"), chat = card.closest(".chat[data-chat]");
      if (li && chat) refreshRow(chat, li.getAttribute("data-msg"));
    });
  }

  // Socket messages. A chat row new or changed: {type: "chat", op: "new"|"update", chat_kind, chat_ref, id, ...}; the
  // page fetches that row as HTML rather than drawing the frame's copy itself. Everything else: {type: "event",
  // event_type, html (the activity row), ...}; an approval or question event may have changed a pending card, so
  // those cards are fetched again (deciding on the Approvals page shows on the card, and the other way round).
  function onChatFrame(msg) {
    if (!msg.chat_kind || msg.chat_ref == null || !msg.id) return;
    openChats(msg.chat_kind + ":" + msg.chat_ref).forEach(function (chat) { refreshRow(chat, msg.id); });
  }
  function onChatEvent(msg) {
    var t = msg.event_type || "";
    if (t.indexOf("approval.") === 0) refreshCards(".ccard-approval.is-pending");
    else if (t.indexOf("question.") === 0) refreshCards(".ccard-question.is-pending");
    else if (t === "chat.message") openChats().forEach(catchUp);  // a hub that sends no chat frames
  }

  // The message box and the cards' forms post with fetch and get their row back; the socket's copy of the same
  // row is then a no-op. Without JavaScript they post and come back to the page.
  document.addEventListener("submit", function (evt) {
    var form = evt.target;
    if (!form.matches || !form.matches("form[data-chat-form]") || !window.fetch) return;
    evt.preventDefault();
    var chat = form.closest(".chat[data-chat]");
    var button = evt.submitter || form.querySelector("button[type=submit]");
    var data = new URLSearchParams(new FormData(form));
    if (evt.submitter && evt.submitter.name) data.set(evt.submitter.name, evt.submitter.value);
    var old = form.nextElementSibling;
    if (old && old.matches("p.error[role=alert], p.sent-note")) old.remove();
    form.querySelectorAll("button").forEach(function (b) { b.disabled = true; });
    fetch(form.getAttribute("action"), {
      method: "POST", credentials: "same-origin", body: data,
      headers: { "X-Chat": "1", "Content-Type": "application/x-www-form-urlencoded" },
    }).then(function (r) {
      return r.text().then(function (text) {
        if (!r.ok) throw { status: r.status, text: text };
        if (form.hasAttribute("data-chat-send")) {
          form.reset();
          forgetDrafts(form);
          sizeBox(form.querySelector("textarea"));
          var log = chat && logOf(chat);
          if (log) log.scrollTop = log.scrollHeight;  // your own message: go to it
        }
        var placed = chat ? upsertRows(chat, text, false) : 0;
        if (!placed && form.isConnected) {  // it went to another chat: say so where the operator wrote it
          form.reset();
          var sent = document.createElement("p");
          sent.className = "small-line sent-note";
          sent.setAttribute("role", "status");
          sent.textContent = "Sent: it's in your chat with the agent.";
          form.after(sent);
        }
      });
    }).catch(function (e) {
      var note = document.createElement("p");
      note.className = "error";
      note.setAttribute("role", "alert");
      var detail = e && e.text ? errorDetail({ responseText: e.text }) : "";
      note.textContent = e && e.status ? "That didn't go through (error " + e.status + ")" + (detail ? ": " + detail : ".")
        : "That didn't go through: the hub can't be reached.";
      form.after(note);
    }).then(function () {
      if (form.isConnected) form.querySelectorAll("button").forEach(function (b) { b.disabled = false; });
      if (button && button.isConnected && form.hasAttribute("data-chat-send")) form.querySelector("textarea").focus();
    });
  });

  // The message box grows with its text (up to the CSS max-height). Enter sends where there is a real keyboard
  // (Shift+Enter for a new line); on a touch screen Enter is a new line and the arrow button sends.
  function sizeBox(ta) {
    if (!ta) return;
    ta.style.height = "auto";
    ta.style.height = ta.scrollHeight + 2 + "px";
    ta.classList.toggle("is-tall", ta.scrollHeight > ta.clientHeight + 2);  // at its max-height: let it scroll
  }
  var fineKeys = window.matchMedia ? window.matchMedia("(hover: hover) and (pointer: fine)") : { matches: true };
  document.addEventListener("input", function (evt) {
    if (evt.target.matches && evt.target.matches(".chatbox textarea, .card-reply textarea")) sizeBox(evt.target);
  });
  document.addEventListener("keydown", function (evt) {
    var ta = evt.target;
    if (evt.key !== "Enter" || evt.isComposing || !ta.matches || !ta.matches(".chatbox textarea, .card-reply textarea")) return;
    if (evt.shiftKey || !(fineKeys.matches || evt.ctrlKey || evt.metaKey)) return;
    evt.preventDefault();
    if (ta.value.trim() && ta.form) ta.form.requestSubmit();
  });

  // @ in a chat's message box (a textarea[data-mentions]: the chat's agents' names as written, JSON). Typing @ and the
  // start of a name lists the names starting with it, case ignored; Up/Down pick, Enter or Tab puts the name in as
  // written, Escape closes the list. Names go into the list as text only.
  var MENTION_AT = /(^|[^\w@])@([A-Za-z0-9_-]{0,64})$/;
  var mention = null;  // the open list: {ta, list, names, sel, typed}
  var mentionSeq = 0;

  function mentionNames(ta) {
    try {
      var v = JSON.parse(ta.getAttribute("data-mentions") || "[]");
      return Array.isArray(v) ? v.filter(function (n) { return typeof n === "string"; }) : [];
    } catch (e) { return []; }
  }
  function closeMentions() {
    if (!mention) return;
    mention.list.remove();
    mention.ta.removeAttribute("aria-controls");
    mention.ta.removeAttribute("aria-activedescendant");
    mention = null;
  }
  function selectMention(i) {
    var items = mention.list.children;
    mention.sel = (i + items.length) % items.length;
    Array.prototype.forEach.call(items, function (li, j) { li.setAttribute("aria-selected", String(j === mention.sel)); });
    mention.ta.setAttribute("aria-activedescendant", items[mention.sel].id);
    items[mention.sel].scrollIntoView({ block: "nearest" });
  }
  function pickMention(name) {
    var ta = mention.ta, end = ta.selectionStart, start = end - mention.typed.length - 1;
    closeMentions();
    ta.setRangeText("@" + name + " ", start, end, "end");
    ta.dispatchEvent(new Event("input", { bubbles: true }));  // resize the box; the list stays shut (a space ends it)
    ta.focus();
  }
  function showMentions(ta) {
    var m = ta.selectionStart === ta.selectionEnd && MENTION_AT.exec(ta.value.slice(0, ta.selectionStart));
    var typed = m ? m[2] : "", key = typed.toLowerCase();
    var hits = m ? mentionNames(ta).filter(function (n) { return n.toLowerCase().indexOf(key) === 0; }) : [];
    if (!hits.length || hits.length === 1 && hits[0].toLowerCase() === key) { closeMentions(); return; }
    if (mention && mention.ta !== ta) closeMentions();
    if (!mention) {
      var list = document.createElement("ul");
      list.className = "mention-list";
      list.id = "mention-list-" + (++mentionSeq);
      list.setAttribute("role", "listbox");
      list.setAttribute("aria-label", "Agents in this chat");
      list.addEventListener("mousedown", function (evt) {
        var li = evt.target.closest("li[role=option]");
        evt.preventDefault();  // keep the focus (and the caret) in the box
        if (li && mention) pickMention(li.getAttribute("data-name"));
      });
      ta.after(list);
      ta.setAttribute("aria-controls", list.id);
      mention = { ta: ta, list: list, sel: 0 };
    }
    mention.typed = typed;
    mention.list.replaceChildren.apply(mention.list, hits.map(function (name, i) {
      var li = document.createElement("li");
      li.id = mention.list.id + "-" + i;
      li.setAttribute("role", "option");
      li.setAttribute("data-name", name);
      li.textContent = "@" + name;
      return li;
    }));
    selectMention(0);
  }
  document.addEventListener("input", function (evt) {
    if (evt.target.matches && evt.target.matches("textarea[data-mentions]")) showMentions(evt.target);
  });
  document.addEventListener("keyup", function (evt) {
    if (/^(ArrowLeft|ArrowRight|Home|End)$/.test(evt.key) && evt.target.matches && evt.target.matches("textarea[data-mentions]")) {
      showMentions(evt.target);
    }
  });
  document.addEventListener("focusout", function (evt) { if (mention && evt.target === mention.ta) closeMentions(); });
  document.addEventListener("submit", function () { closeMentions(); }, true);
  // Capture phase: while the list is open these keys are its own (not Send, not closing the panel).
  document.addEventListener("keydown", function (evt) {
    if (!mention || evt.target !== mention.ta || evt.isComposing) return;
    if (evt.key === "ArrowDown" || evt.key === "ArrowUp") selectMention(mention.sel + (evt.key === "ArrowDown" ? 1 : -1));
    else if ((evt.key === "Enter" || evt.key === "Tab") && !evt.shiftKey) pickMention(mention.list.children[mention.sel].getAttribute("data-name"));
    else if (evt.key === "Escape") closeMentions();
    else return;
    evt.preventDefault();
    evt.stopPropagation();
  }, true);

  // Drafts: a message box with data-draft-key (a chat's: "agent:<id>", "task:<id>", "project:<name>", "dm:<ref>"; the
  // device page's broadcast box: "device:<node_id>") keeps what the operator typed in sessionStorage, per chat and
  // per tab, so it survives the panel closing, another chat opening in it, a swap re-rendering it, and leaving the
  // page and coming back. Restored into an empty box wherever one with that key appears (page load, anything HTMX
  // swaps in). Forgotten only once the hub has taken the message (the 2xx paths above call forgetDrafts); a refusal
  // or a failed send keeps the box and the draft as they are. Emptying the box by hand forgets it too. Storage that
  // is off or full (private browsing) just means no drafts: every access is guarded.
  var DRAFT = "agenthub.draft.";
  function draftKey(ta) { return DRAFT + ta.getAttribute("data-draft-key"); }
  function saveDraft(ta) {
    try {
      if (ta.value.trim()) sessionStorage.setItem(draftKey(ta), ta.value);
      else sessionStorage.removeItem(draftKey(ta));
    } catch (e) { /* no storage: no drafts */ }
  }
  function forgetDrafts(form) {
    if (!form || !form.querySelectorAll) return;
    form.querySelectorAll("textarea[data-draft-key]").forEach(function (ta) {
      try { sessionStorage.removeItem(draftKey(ta)); } catch (e) { /* no storage */ }
    });
  }
  function restoreDrafts(root) {
    if (!root || !root.querySelectorAll) return;
    var boxes = Array.prototype.slice.call(root.querySelectorAll("textarea[data-draft-key]"));
    if (root.matches && root.matches("textarea[data-draft-key]")) boxes.push(root);
    boxes.forEach(function (ta) {
      if (ta.value) return;  // hx-preserve kept the element, or the browser refilled it: that text wins
      var text = null;
      try { text = sessionStorage.getItem(draftKey(ta)); } catch (e) { return; }
      if (!text) return;
      ta.value = text;
      if (ta.closest(".chatbox")) sizeBox(ta);
    });
  }
  document.addEventListener("input", function (evt) {
    if (evt.target.matches && evt.target.matches("textarea[data-draft-key]")) saveDraft(evt.target);
  });
  // Without HTMX the broadcast box posts as a plain form and the page comes back with its "Sent to N" line: the hub
  // took that message, so its draft goes before restoring would put it back. (With HTMX a "Sent" line on load is
  // only an old ?sent= address revisited, not this draft being sent.)
  if (!window.htmx) document.querySelectorAll(".broadcast").forEach(function (box) {
    if (box.querySelector("#broadcast-result .ok-line")) forgetDrafts(box.querySelector("form"));
  });
  if (window.htmx) htmx.onLoad(restoreDrafts); else restoreDrafts(document.body);

  // A chat on the page (not in the panel) starts at its newest row too.
  openChats().forEach(showLatest);

  var feed = document.getElementById("feed");
  var live = document.getElementById("ws-state");
  var filters = feed ? { project: feed.dataset.project, agent: feed.dataset.agent, type: feed.dataset.type } : {};
  var delay = 1000, socketUp = false;

  function setState(state) {
    if (live) { live.dataset.state = state; live.textContent = state; }
  }

  function matches(msg) {
    if (filters.project && msg.project !== filters.project) return false;
    if (filters.agent && msg.actor !== filters.agent) return false;
    if (filters.type && msg.event_type.indexOf(filters.type) !== 0) return false;
    return true;
  }

  function onFeedEvent(msg) {
    if (!feed || !matches(msg)) return;
    // msg.html is rendered and escaped by the server's templates.
    feed.insertAdjacentHTML("afterbegin", msg.html);
    if (feed.firstElementChild) feed.firstElementChild.classList.add("new");
    while (feed.children.length > 300) feed.removeChild(feed.lastElementChild);
  }

  function connect() {
    var scheme = location.protocol === "https:" ? "wss://" : "ws://";
    var ws = new WebSocket(scheme + location.host + "/ui/ws");
    var ping;
    ws.onopen = function () {
      setState("live");
      if (delay > 1000) openChats().forEach(catchUp);  // back after a drop: fetch what was missed
      delay = 1000;
      socketUp = true;
      ping = setInterval(function () { ws.send("ping"); }, 25000);
    };
    ws.onmessage = function (evt) {
      var msg;
      try { msg = JSON.parse(evt.data); } catch (e) { return; }
      if (msg.type === "chat") { onChatFrame(msg); return; }
      if (msg.type !== "event") return;
      onChatEvent(msg);
      onFeedEvent(msg);
    };
    ws.onclose = function () {
      clearInterval(ping);
      socketUp = false;
      setState("offline");
      setTimeout(connect, delay);
      delay = Math.min(delay * 2, 30000);
    };
  }
  if (window.WebSocket) connect();
  setInterval(function () { if (!socketUp) openChats().forEach(catchUp); }, 10000);
})();
