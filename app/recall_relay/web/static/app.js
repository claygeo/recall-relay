/* Recall Relay — the only client-side code in the product.
   Two jobs: consume the scan's SSE stream into the run log and tally, and POST the handful of controls
   that change state, then follow the server's redirect. No framework, no router, no state machine. */
(function () {
  "use strict";

  /* ------------------------------------------------------------ helpers */
  function el(id) { return document.getElementById(id); }

  function flash(message, headline) {
    var box = el("flash");
    if (!box) { window.alert(message); return; }
    box.innerHTML = "";
    var eyebrow = document.createElement("span");
    eyebrow.className = "eyebrow";
    eyebrow.textContent = headline || "Not done";
    var body = document.createElement("div");
    body.textContent = message;
    box.appendChild(eyebrow);
    box.appendChild(body);
    box.hidden = false;
    box.scrollIntoView({ block: "nearest" });
  }

  async function post(url, body) {
    var options = { method: "POST", headers: { Accept: "application/json" } };
    if (body instanceof FormData) {
      options.body = body;
    } else if (body) {
      options.headers["Content-Type"] = "application/json";
      options.body = JSON.stringify(body);
    }
    var response = await fetch(url, options);
    var payload = null;
    try { payload = await response.json(); } catch (err) { payload = null; }
    if (!response.ok) {
      var message = (payload && (payload.message || payload.detail)) ||
        "The server refused that (HTTP " + response.status + ").";
      throw { message: message, headline: (payload && payload.headline) || "Not done", status: response.status };
    }
    return payload || {};
  }

  /* --------------------------------------------------- POST control bar */
  document.addEventListener("click", function (event) {
    var trigger = event.target.closest("[data-post]");
    if (!trigger) return;
    event.preventDefault();
    if (trigger.dataset.confirm && !window.confirm(trigger.dataset.confirm)) return;

    var payload = trigger.dataset.payload ? JSON.parse(trigger.dataset.payload) : null;
    trigger.setAttribute("aria-disabled", "true");
    post(trigger.dataset.post, payload).then(function (result) {
      if (result && result.redirect) { window.location.href = result.redirect; return; }
      window.location.reload();
    }).catch(function (err) {
      trigger.removeAttribute("aria-disabled");
      flash(err.message || String(err), err.headline);
    });
  });

  /* ------------------------------------------------------- the scan run */
  var TALLY_KEYS = ["seen", "skipped_non_food", "already_seen", "dismissed", "needs_human", "awaiting_approval", "blocked"];

  function setTally(tally) {
    TALLY_KEYS.forEach(function (key) {
      var node = el("t-" + key);
      if (!node) return;
      var value = tally[key] || 0;
      node.textContent = String(value);
      node.classList.toggle("zero", value === 0);
    });
    var errors = el("t-errors");
    if (errors) {
      errors.textContent = String(tally.errors || 0);
      errors.classList.toggle("zero", !tally.errors);
    }
  }

  function line(text, kind) {
    var log = el("log");
    if (!log) return;
    var li = document.createElement("li");
    if (kind) li.className = "is-" + kind;
    var label = document.createElement("span");
    label.className = "k";
    label.textContent = (kind || "log").toUpperCase();
    li.appendChild(label);
    li.appendChild(document.createTextNode(text));
    log.appendChild(li);
    log.scrollTop = log.scrollHeight;
  }

  function live(on) {
    var dot = el("livedot");
    if (dot) dot.classList.toggle("on", !!on);
    var label = el("livelabel");
    if (label) label.textContent = on ? "streaming" : "idle";
    var button = el("run-scan");
    if (button) {
      if (on) button.setAttribute("aria-disabled", "true");
      else button.removeAttribute("aria-disabled");
    }
  }

  function render(event, tally) {
    switch (event.type) {
      case "feed":
        if (event.error) line(event.source + " feed unavailable: " + event.error, "error");
        else line(event.source + " feed · " + event.items + " item(s) · " + (event.url || ""), "feed");
        break;
      case "fetch":
        line((event.blocked ? "blocked " : "") + event.origin + " · " + event.link, "fetch");
        break;
      case "tool":
        line(event.name + (event.case_id ? " · " + event.case_id : ""), "tool");
        break;
      case "verdict":
        line(event.verdict + " rows=" + JSON.stringify(event.receipt_ids || []) +
          (event.widening_applied ? " (widened)" : "") + " · " + (event.reason || ""), "verdict");
        break;
      case "ping":
        line(event.case_id + " · " + event.text, "ping");
        break;
      case "item":
        if (event.decision === "already_seen") tally.already_seen += 1;
        if (event.decision === "skipped_non_food") tally.skipped_non_food += 1;
        if (event.decision === "dismissed") tally.dismissed += 1;
        if (event.decision === "blocked") tally.blocked = (tally.blocked || 0) + 1;
        if (event.decision === "error") tally.errors += 1;
        if (event.status === "needs_human") tally.needs_human += 1;
        if (event.status === "awaiting_approval") tally.awaiting_approval += 1;
        if (event.index > tally.seen) tally.seen = event.index;
        setTally(tally);
        line(event.index + "/" + event.total + " " + event.decision.replace(/_/g, " ") +
          " · " + (event.title || event.link || "") + (event.case_id ? " · " + event.case_id : ""),
          event.decision === "error" ? "error" : "item");
        break;
      case "done":
        setTally(event.tally || tally);
        line("scan complete · " + JSON.stringify(event.tally || tally), "done");
        break;
      default:
        line(JSON.stringify(event), "log");
    }
  }

  function attach(jobId) {
    var tally = { seen: 0, skipped_non_food: 0, already_seen: 0, dismissed: 0, needs_human: 0,
                  awaiting_approval: 0, errors: 0 };
    setTally(tally);
    live(true);
    var source = new EventSource("/api/scan/stream?job=" + encodeURIComponent(jobId));
    source.onmessage = function (message) {
      var event;
      try { event = JSON.parse(message.data); } catch (err) { return; }
      if (event.type === "closed") {
        source.close();
        live(false);
        var cases = el("run-cases");
        if (cases) cases.hidden = false;
        return;
      }
      render(event, tally);
    };
    source.onerror = function () {
      source.close();
      live(false);
      line("stream closed", "done");
    };
  }

  var runButton = el("run-scan");
  if (runButton) {
    runButton.addEventListener("click", function (event) {
      event.preventDefault();
      if (runButton.getAttribute("aria-disabled") === "true") return;
      var log = el("log");
      if (log) log.innerHTML = "";
      live(true);
      post("/api/scan", null).then(function (result) {
        line("job " + result.job + " started", "feed");
        attach(result.job);
      }).catch(function (err) {
        live(false);
        flash(err.message || String(err), err.headline);
      });
    });
  }

  var pending = document.body ? document.body.dataset.activeJob : "";
  if (pending) {
    attach(pending);
  } else if (runButton && runButton.dataset.lastJob) {
    // No scan in flight: replay the last finished run so the tally and log survive a refresh.
    var log = el("log");
    if (log) log.innerHTML = "";
    line("replaying the last completed run (job " + runButton.dataset.lastJob + ")", "feed");
    attach(runButton.dataset.lastJob);
  }
})();
