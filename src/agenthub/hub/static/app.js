// agenthub dashboard: live feed over WebSocket and small HTMX niceties.
// No inline scripts or eval: the CSP only allows scripts from /ui/static.
(function () {
  "use strict";

  // Clear chat forms after a successful HTMX post.
  document.addEventListener("htmx:afterRequest", function (evt) {
    var form = evt.detail.elt;
    if (form && form.hasAttribute && form.hasAttribute("data-reset") && evt.detail.successful) form.reset();
  });

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
