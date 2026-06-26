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
