// Tabbed panels: clicking a .tab shows the .tab-panel whose id matches the
// button's data-tab and hides the rest. Server renders the first as .active,
// so the page is usable before this runs. This script also wires up the ARIA
// tab pattern (roles, aria-selected, roving tabindex, arrow-key navigation).
(function () {
  "use strict";
  function own(group, selector) {
    // querySelectorAll is recursive; keep only elements belonging to THIS group
    // (not a nested [data-tabs]) so sub-tabs and outer tabs stay independent.
    return Array.prototype.filter.call(
      group.querySelectorAll(selector),
      function (el) { return el.closest("[data-tabs]") === group; });
  }
  document.querySelectorAll("[data-tabs]").forEach(function (group) {
    var tabs = own(group, ".tab");
    var panels = own(group, ".tab-panel");

    function select(tab, focus) {
      var id = tab.dataset.tab;
      tabs.forEach(function (t) {
        var on = t === tab;
        t.classList.toggle("active", on);
        t.setAttribute("aria-selected", on ? "true" : "false");
        t.setAttribute("tabindex", on ? "0" : "-1");
      });
      panels.forEach(function (p) { p.classList.toggle("active", p.id === id); });
      if (focus) tab.focus();
    }

    tabs.forEach(function (tab, i) {
      tab.setAttribute("role", "tab");
      tab.setAttribute("aria-controls", tab.dataset.tab);
      var on = tab.classList.contains("active");
      tab.setAttribute("aria-selected", on ? "true" : "false");
      tab.setAttribute("tabindex", on ? "0" : "-1");
      tab.addEventListener("click", function () { select(tab); });
      tab.addEventListener("keydown", function (e) {
        var to = null;
        if (e.key === "ArrowRight") to = tabs[(i + 1) % tabs.length];
        else if (e.key === "ArrowLeft") to = tabs[(i - 1 + tabs.length) % tabs.length];
        else if (e.key === "Home") to = tabs[0];
        else if (e.key === "End") to = tabs[tabs.length - 1];
        if (to) { e.preventDefault(); select(to, true); }
      });
    });
    panels.forEach(function (p) { p.setAttribute("role", "tabpanel"); });
  });
})();
