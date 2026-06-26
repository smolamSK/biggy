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
  ["bulk-delete", "bulk-export"].forEach(function (id) {
    var btn = document.getElementById(id);
    if (!btn) return;
    btn.addEventListener("click", function (e) {
      if (selected().length === 0) {
        e.preventDefault();
        alert("Select at least one row first.");
      }
    });
  });

  refresh();
})();
