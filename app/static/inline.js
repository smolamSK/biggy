// User-mode list: click a cell to edit it in place (scalar / enum / boolean
// fields only — relations and the display column stay on the edit form).
// Posts to the cell endpoint and swaps in the returned display value.
(function () {
  "use strict";
  var table = document.getElementById("list-table");
  if (!table) return;

  function csrf() {
    var el = document.querySelector('#bulk-form input[name="csrf_token"]');
    return el ? el.value : "";
  }

  var INPUT_TYPE = {
    integer: "number", bigint: "number", decimal: "number", float: "number",
    currency: "number", percent: "number",
    email: "email", url: "url", phone: "tel",
    date: "date", datetime: "datetime-local", time: "time"
  };

  // Value -> status-chip hue: the field's designer-chosen colors (the cell's
  // data-colors JSON map) win; otherwise the deterministic hash mirroring
  // chip_hue() in app/__init__.py (char-code sum mod 7).
  var CHIP_HUES = ["green", "amber", "red", "blue", "violet", "teal", "gray"];
  function chipHue(s, colors) {
    if (!s) return "gray";
    if (colors && CHIP_HUES.indexOf(colors[s]) !== -1) return colors[s];
    var sum = 0;
    for (var i = 0; i < s.length; i++) sum += s.charCodeAt(i);
    return CHIP_HUES[sum % CHIP_HUES.length];
  }

  // Write a returned display value back into a cell — as a chip for enums.
  function renderCell(cell, display) {
    if (cell.dataset.type === "enum" && display) {
      var colors = null;
      try { colors = JSON.parse(cell.dataset.colors || "null"); } catch (e) { /* auto */ }
      var chip = document.createElement("span");
      chip.className = "chip c-" + chipHue(display, colors);
      chip.textContent = display;
      cell.textContent = "";
      cell.appendChild(chip);
    } else {
      cell.textContent = display;
    }
  }

  function makeControl(cell) {
    var type = cell.dataset.type, value = cell.dataset.value || "";
    var el;
    if (type === "boolean") {
      el = document.createElement("select");
      [["1", "yes"], ["0", "no"]].forEach(function (o) {
        var opt = new Option(o[1], o[0]); el.add(opt);
      });
      el.value = value === "1" ? "1" : "0";
    } else if (type === "enum") {
      el = document.createElement("select");
      if (!cell.dataset.noblank) el.add(new Option("— none —", ""));
      var opts = [];
      try { opts = JSON.parse(cell.dataset.enum || "[]"); } catch (e) { opts = []; }
      opts.forEach(function (o) { el.add(new Option(o, o)); });
      el.value = value;
    } else if (type === "text") {
      el = document.createElement("textarea");
      el.rows = 2; el.value = value;
    } else {
      el = document.createElement("input");
      el.type = INPUT_TYPE[type] || "text";
      if (type === "decimal" || type === "float") el.step = "any";
      el.value = value;
    }
    el.style.width = "100%";
    return el;
  }

  function edit(cell) {
    if (cell.querySelector("input, select, textarea")) return; // already editing
    var original = cell.innerHTML;
    var control = makeControl(cell);
    cell.innerHTML = "";
    cell.appendChild(control);
    control.focus();

    var done = false;
    function cancel() { if (!done) { done = true; cell.innerHTML = original; } }
    function save() {
      if (done) return;
      done = true;
      var body = new URLSearchParams();
      body.set("csrf_token", csrf());
      body.set("col", cell.dataset.col);
      body.set("value", control.value);
      fetch(cell.dataset.url, {
        method: "POST", headers: { "X-Requested-With": "fetch" }, body: body
      }).then(function (r) {
        return r.json().then(function (data) { return { ok: r.ok, data: data }; });
      }).then(function (res) {
        if (res.ok && res.data.ok) {
          cell.dataset.value = res.data.value;
          renderCell(cell, res.data.display);
        } else {
          cell.innerHTML = original;
          alert((res.data && res.data.error) || "Could not save.");
        }
      }).catch(function () {
        cell.innerHTML = original;
        alert("Could not save (network error).");
      });
    }

    control.addEventListener("keydown", function (e) {
      if (e.key === "Enter" && control.tagName !== "TEXTAREA") { e.preventDefault(); save(); }
      else if (e.key === "Escape") { e.preventDefault(); cancel(); }
    });
    control.addEventListener("blur", save);
  }

  table.addEventListener("click", function (e) {
    var cell = e.target.closest("td[data-editable]");
    if (cell && !cell.querySelector("input, select, textarea")) edit(cell);
  });
})();
