// User-mode list: per-user column show/hide + reorder, persisted in
// localStorage (key biggy.cols.<formId>). Purely presentational — it never
// touches the server, so CSV export / bulk actions keep the canonical order.
(function () {
  "use strict";
  var table = document.getElementById("list-table");
  var panel = document.getElementById("columns-panel");
  var btn = document.getElementById("columns-btn");
  if (!table || !panel || !btn) return;

  var key = "biggy.cols." + (panel.dataset.formId || "0");

  // Canonical columns from the header (data-col + visible label).
  var header = table.rows[0];
  var all = [];
  Array.prototype.forEach.call(header.cells, function (th) {
    if (th.dataset.col) all.push({ col: th.dataset.col, label: th.textContent.trim() });
  });

  function load() {
    try {
      var s = JSON.parse(localStorage.getItem(key) || "{}");
      return { order: Array.isArray(s.order) ? s.order : [], hidden: Array.isArray(s.hidden) ? s.hidden : [] };
    } catch (e) { return { order: [], hidden: [] }; }
  }
  function save(state) { localStorage.setItem(key, JSON.stringify(state)); }

  function orderedCols(state) {
    var known = all.map(function (c) { return c.col; });
    var seq = state.order.filter(function (c) { return known.indexOf(c) !== -1; });
    known.forEach(function (c) { if (seq.indexOf(c) === -1) seq.push(c); });
    return seq;
  }

  function apply(state) {
    var seq = orderedCols(state);
    var hidden = {};
    state.hidden.forEach(function (c) { hidden[c] = true; });
    Array.prototype.forEach.call(table.rows, function (row) {
      var last = row.cells[row.cells.length - 1]; // Actions column stays last
      seq.forEach(function (col) {
        var cell = row.querySelector('[data-col="' + col.replace(/"/g, '\\"') + '"]');
        if (cell) row.insertBefore(cell, last);
      });
      Array.prototype.forEach.call(row.querySelectorAll("[data-col]"), function (cell) {
        cell.style.display = hidden[cell.dataset.col] ? "none" : "";
      });
    });
  }

  function render(state) {
    var seq = orderedCols(state);
    var hidden = {};
    state.hidden.forEach(function (c) { hidden[c] = true; });
    var labels = {};
    all.forEach(function (c) { labels[c.col] = c.label; });

    panel.innerHTML = "";
    var box = document.createElement("div");
    box.className = "panel";
    box.style.cssText = "max-width:320px;margin:.25rem 0;padding:.5rem .75rem";
    seq.forEach(function (col, i) {
      var row = document.createElement("div");
      row.style.cssText = "display:flex;align-items:center;gap:.4rem;padding:.15rem 0";
      var cb = document.createElement("input");
      cb.type = "checkbox"; cb.checked = !hidden[col];
      cb.addEventListener("change", function () {
        state.hidden = seq.filter(function (c) {
          return c === col ? cb.checked === false : hidden[c];
        });
        save(state); apply(state); render(state);
      });
      var name = document.createElement("span");
      name.textContent = labels[col] || col; name.style.flex = "1";
      var up = mkBtn("↑", i === 0, function () { move(state, seq, i, -1); });
      var down = mkBtn("↓", i === seq.length - 1, function () { move(state, seq, i, 1); });
      row.appendChild(cb); row.appendChild(name); row.appendChild(up); row.appendChild(down);
      box.appendChild(row);
    });
    var reset = document.createElement("button");
    reset.type = "button"; reset.className = "btn sm"; reset.textContent = "Reset";
    reset.style.marginTop = ".4rem";
    reset.addEventListener("click", function () {
      localStorage.removeItem(key); location.reload();
    });
    box.appendChild(reset);
    panel.appendChild(box);
  }

  function mkBtn(text, disabled, fn) {
    var b = document.createElement("button");
    b.type = "button"; b.className = "btn sm"; b.textContent = text; b.disabled = disabled;
    b.addEventListener("click", fn);
    return b;
  }

  function move(state, seq, i, delta) {
    var j = i + delta;
    if (j < 0 || j >= seq.length) return;
    var tmp = seq[i]; seq[i] = seq[j]; seq[j] = tmp;
    state.order = seq;
    save(state); apply(state); render(state);
  }

  var state = load();
  apply(state);
  render(state);
  btn.addEventListener("click", function () {
    panel.hidden = !panel.hidden;
  });
})();
