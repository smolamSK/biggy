// Progressive enhancement for the designer "add field" form:
// show only the inputs relevant to the selected data type.
(function () {
  function sync(form) {
    var type = form.querySelector('[name="data_type"]');
    if (!type) return;
    var show = {
      length: ["string"],
      precision: ["decimal"],
      scale: ["decimal"],
      enum_options: ["enum"],
    };
    Object.keys(show).forEach(function (name) {
      var input = form.querySelector('[name="' + name + '"]');
      if (!input) return;
      var wrap = input.closest(".field") || input.parentElement;
      wrap.style.display = show[name].indexOf(type.value) === -1 ? "none" : "";
    });
  }
  document.addEventListener("DOMContentLoaded", function () {
    document.querySelectorAll('form').forEach(function (form) {
      var type = form.querySelector('[name="data_type"]');
      if (!type) return;
      sync(form);
      type.addEventListener("change", function () { sync(form); });
    });
  });
})();

// Collapsible User-mode menu groups, with state persisted across navigations.
(function () {
  var KEY = "biggy.menu.collapsed"; // { menuId: true } for collapsed groups
  function load() {
    try { return JSON.parse(localStorage.getItem(KEY)) || {}; } catch (e) { return {}; }
  }
  function save(state) {
    try { localStorage.setItem(KEY, JSON.stringify(state)); } catch (e) { /* ignore */ }
  }
  document.addEventListener("DOMContentLoaded", function () {
    var groups = document.querySelectorAll("details.menu-group");
    if (!groups.length) return;
    var state = load();
    groups.forEach(function (d) {
      var id = d.getAttribute("data-menu-id");
      if (id in state) d.open = !state[id]; // stored true == collapsed
      d.addEventListener("toggle", function () {
        var s = load();
        s[id] = !d.open;
        save(s);
      });
    });
    function setAll(open) {
      var s = load();
      groups.forEach(function (d) {
        d.open = open;
        s[d.getAttribute("data-menu-id")] = !open;
      });
      save(s);
    }
    var ex = document.getElementById("menu-expand-all");
    var col = document.getElementById("menu-collapse-all");
    if (ex) ex.addEventListener("click", function (e) { e.preventDefault(); setAll(true); });
    if (col) col.addEventListener("click", function (e) { e.preventDefault(); setAll(false); });
  });
})();

// ---- Liveness: auto-dismiss flashes, live badges, unsaved-changes guard ----
(function () {
  "use strict";
  document.addEventListener("DOMContentLoaded", function () {
    // flashes: success/info fade out; danger/warning stay but get a dismiss ✕
    document.querySelectorAll(".flash").forEach(function (f) {
      var x = document.createElement("button");
      x.className = "flash-x";
      x.type = "button";
      x.textContent = "×";
      x.title = "Dismiss";
      x.addEventListener("click", function () { f.remove(); });
      f.appendChild(x);
      if (f.classList.contains("success") || f.classList.contains("info")) {
        setTimeout(function () {
          f.classList.add("flash-out");
          setTimeout(function () { f.remove(); }, 400);
        }, 6000);
      }
    });

    // live 🔔 / ✓ badges: poll a tiny JSON endpoint while the tab is visible
    var bell = document.getElementById("badge-notif");
    var appr = document.getElementById("badge-appr");
    function setBadge(anchor, n) {
      if (!anchor) return;
      var b = anchor.querySelector(".badge");
      if (n > 0) {
        if (!b) {
          b = document.createElement("span");
          b.className = "badge";
          anchor.appendChild(document.createTextNode(" "));
          anchor.appendChild(b);
        }
        b.textContent = n;
      } else if (b) {
        b.remove();
      }
    }
    if (bell || appr) {
      setInterval(function () {
        if (document.visibilityState !== "visible") return;
        fetch("/u/badges", { headers: { Accept: "application/json" } })
          .then(function (r) { return r.ok ? r.json() : null; })
          .then(function (d) {
            if (!d) return;
            setBadge(bell, d.notifications);
            setBadge(appr, d.approvals);
          })
          .catch(function () { /* offline — try again next tick */ });
      }, 60000);
    }

    // unsaved-changes guard on forms marked data-guard
    document.querySelectorAll("form[data-guard]").forEach(function (form) {
      var dirty = false;
      form.addEventListener("input", function () { dirty = true; });
      form.addEventListener("change", function () { dirty = true; });
      form.addEventListener("submit", function () { dirty = false; });
      window.addEventListener("beforeunload", function (e) {
        if (dirty) { e.preventDefault(); e.returnValue = ""; }
      });
    });

    // dropdown menus (details.menu): close when clicking elsewhere or on Esc
    document.addEventListener("click", function (e) {
      document.querySelectorAll("details.menu[open]").forEach(function (d) {
        if (!d.contains(e.target)) d.removeAttribute("open");
      });
    });
    document.addEventListener("keydown", function (e) {
      if (e.key !== "Escape") return;
      document.querySelectorAll("details.menu[open]").forEach(function (d) {
        d.removeAttribute("open");
      });
    });

    // mobile: ☰ toggles the off-canvas sidebar (scrim click closes)
    var burger = document.getElementById("nav-burger");
    var sidebar = document.querySelector(".sidebar");
    if (burger && sidebar) {
      var scrim = document.createElement("div");
      scrim.className = "nav-scrim";
      document.body.appendChild(scrim);
      function toggle(open) {
        document.body.classList.toggle("sidebar-open", open);
      }
      burger.addEventListener("click", function () {
        toggle(!document.body.classList.contains("sidebar-open"));
      });
      scrim.addEventListener("click", function () { toggle(false); });
    }
  });
})();
