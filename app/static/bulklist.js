// User-mode list: "select all" toggle, selected-count display, and a guard
// against submitting a bulk action with nothing selected.
(function () {
  "use strict";
  var table = document.getElementById("list-table");
  var form = document.getElementById("bulk-form");
  if (!table || !form) return;

  var selectAll = document.getElementById("select-all");
  var count = document.getElementById("bulk-count");

  function boxes() {
    return Array.prototype.slice.call(form.querySelectorAll('input[name="ids"]'));
  }
  function selected() {
    return boxes().filter(function (b) { return b.checked; });
  }
  function refresh() {
    var all = boxes(), sel = selected();
    if (selectAll) {
      selectAll.checked = all.length > 0 && sel.length === all.length;
      selectAll.indeterminate = sel.length > 0 && sel.length < all.length;
    }
    var actions = document.getElementById("bulk-actions");
    if (actions) actions.hidden = sel.length === 0;
    all.forEach(function (box) { box.closest("tr").classList.toggle("is-selected", box.checked); });
    if (count) count.textContent = sel.length ? sel.length + " selected" : "";
  }

  if (selectAll) {
    selectAll.addEventListener("change", function () {
      boxes().forEach(function (b) { b.checked = selectAll.checked; });
      refresh();
    });
  }
  form.addEventListener("change", function (e) {
    if (e.target && e.target.name === "ids") refresh();
  });

  // Block bulk submit when nothing is selected.
  ["bulk-delete", "bulk-export", "bulk-edit", "bulk-send"].forEach(function (id) {
    var btn = document.getElementById(id);
    if (!btn) return;
    btn.addEventListener("click", function (e) {
      if (selected().length === 0) {
        e.preventDefault();
        alert("Select at least one row first.");
      }
    });
  });

  var density = document.getElementById("table-density");
  if (density) {
    try { density.value = localStorage.getItem("biggy.tableDensity") === "compact" ? "compact" : "comfortable"; } catch (e) {}
    function applyDensity() { table.dataset.density = density.value; }
    density.addEventListener("change", function () {
      applyDensity();
      try { localStorage.setItem("biggy.tableDensity", density.value); } catch (e) {}
    });
    applyDensity();
  }
  window.addEventListener("pageshow", refresh);
  refresh();
})();
