// Tabbed panels: clicking a .tab shows the .tab-panel whose id matches the
// button's data-tab and hides the rest. Server renders the first as .active,
// so the page is usable before this runs.
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
    tabs.forEach(function (tab) {
      tab.addEventListener("click", function () {
        var id = tab.dataset.tab;
        tabs.forEach(function (t) { t.classList.toggle("active", t === tab); });
        panels.forEach(function (p) { p.classList.toggle("active", p.id === id); });
      });
    });
  });
})();
