// Command palette (Ctrl/Cmd+K): jump to any nav destination, recent pages, or
// global record search. Progressive enhancement — every target it offers is a
// plain link that also exists in the sidebar/topbar; loaded only when signed in.
(function () {
  "use strict";
  var RECENT_KEY = "biggy.recent";
  var MAX_RESULTS = 12;

  function loadRecent() {
    try { return JSON.parse(localStorage.getItem(RECENT_KEY)) || []; } catch (e) { return []; }
  }

  // Record the visited page (app pages only — they have a sidebar).
  document.addEventListener("DOMContentLoaded", function () {
    if (!document.querySelector(".sidebar")) return;
    var title = (document.title || "").replace(/ · Biggy$/, "").trim();
    if (!title) return;
    var url = location.pathname + location.search;
    var rec = loadRecent().filter(function (r) { return r.u !== url; });
    rec.unshift({ t: title, u: url });
    try { localStorage.setItem(RECENT_KEY, JSON.stringify(rec.slice(0, 8))); } catch (e) { /* full */ }
  });

  var overlay = null, input = null, list = null;
  var results = [], active = 0, prevFocus = null;

  // Collect palette entries from the page's own navigation.
  function harvest() {
    var out = [], seen = {};
    function add(label, href, hint) {
      if (!label || !href || href.charAt(0) === "#" || seen[label + "|" + href]) return;
      seen[label + "|" + href] = 1;
      out.push({ label: label, href: href, hint: hint || "" });
    }
    var addNew = document.querySelector('a[data-sc="new"]');
    if (addNew) add("New record", addNew.getAttribute("href"), "this list");
    document.querySelectorAll(".sidebar a").forEach(function (a) {
      var grp = a.closest("details.menu-group");
      var g = grp ? (grp.querySelector("summary") || { textContent: "" }).textContent : "";
      add((a.textContent || "").trim(), a.getAttribute("href"), g.trim());
    });
    document.querySelectorAll("header.topbar a").forEach(function (a) {
      var label = (a.textContent || "").trim() || a.getAttribute("title") || "";
      add(label.trim(), a.getAttribute("href"));
    });
    return out;
  }

  function build() {
    var d = document.createElement("div");
    d.id = "palette";
    d.setAttribute("role", "dialog");
    d.setAttribute("aria-modal", "true");
    d.setAttribute("aria-label", "Command palette");
    d.innerHTML =
      '<div class="palette-card">' +
      '<input type="text" placeholder="Go to… (type to filter, Enter to open)" ' +
      'aria-label="Command palette search" autocomplete="off" spellcheck="false">' +
      '<ul class="palette-list" role="listbox"></ul></div>';
    d.addEventListener("mousedown", function (e) { if (e.target === d) close(); });
    document.body.appendChild(d);
    input = d.querySelector("input");
    list = d.querySelector("ul");
    input.addEventListener("input", function () { render(input.value); });
    input.addEventListener("keydown", function (e) {
      if (e.key === "ArrowDown") { e.preventDefault(); move(1); }
      else if (e.key === "ArrowUp") { e.preventDefault(); move(-1); }
      else if (e.key === "Enter") {
        e.preventDefault();
        if (results[active]) window.location.href = results[active].href;
      } else if (e.key === "Escape") { e.preventDefault(); close(); }
    });
    return d;
  }

  function move(delta) {
    if (!results.length) return;
    active = (active + delta + results.length) % results.length;
    paint();
  }

  function paint() {
    Array.prototype.forEach.call(list.children, function (li, i) {
      li.classList.toggle("active", i === active);
      li.setAttribute("aria-selected", i === active ? "true" : "false");
    });
    var cur = list.children[active];
    if (cur && cur.scrollIntoView) cur.scrollIntoView({ block: "nearest" });
  }

  function render(q) {
    q = (q || "").trim().toLowerCase();
    if (q) {
      results = harvest()
        .map(function (it) { return { it: it, pos: it.label.toLowerCase().indexOf(q) }; })
        .filter(function (m) { return m.pos !== -1; })
        .sort(function (a, b) { return a.pos - b.pos; })
        .slice(0, MAX_RESULTS)
        .map(function (m) { return m.it; });
      results.push({
        label: "Search records for “" + q + "”",
        href: "/u/search?q=" + encodeURIComponent(q),
        hint: "everywhere",
      });
    } else {
      results = loadRecent().map(function (r) {
        return { label: r.t, href: r.u, hint: "recent" };
      });
      if (!results.length) results = harvest().slice(0, MAX_RESULTS);
    }
    active = 0;
    list.innerHTML = "";
    results.forEach(function (it, i) {
      var li = document.createElement("li");
      li.setAttribute("role", "option");
      li.id = "pal-opt-" + i;
      var t = document.createElement("span");
      t.textContent = it.label;
      li.appendChild(t);
      if (it.hint) {
        var h = document.createElement("span");
        h.className = "muted";
        h.textContent = it.hint;
        li.appendChild(h);
      }
      li.addEventListener("mousedown", function (e) { e.preventDefault(); });
      li.addEventListener("click", function () { window.location.href = it.href; });
      li.addEventListener("mousemove", function () {
        if (active !== i) { active = i; paint(); }
      });
      list.appendChild(li);
    });
    if (!results.length) list.innerHTML = '<li class="palette-empty">Nothing matches.</li>';
    paint();
  }

  function open() {
    if (!overlay) overlay = build();
    prevFocus = document.activeElement;
    overlay.classList.add("open");
    input.value = "";
    render("");
    input.focus();
  }
  function close() {
    if (overlay) overlay.classList.remove("open");
    if (prevFocus && prevFocus.focus) prevFocus.focus();
  }
  function isOpen() { return overlay && overlay.classList.contains("open"); }

  document.addEventListener("keydown", function (e) {
    if ((e.ctrlKey || e.metaKey) && !e.altKey && (e.key === "k" || e.key === "K")) {
      e.preventDefault();
      if (isOpen()) close(); else open();
    }
  });
})();
