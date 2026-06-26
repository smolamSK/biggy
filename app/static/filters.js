// User-mode list filters: an add-condition builder.
// Reads column metadata + current conditions emitted by the server and renders
// removable (column -> operator -> value) rows. All three controls are always
// present and named fcol/fop/fval so the parallel query-string lists stay aligned.
(function () {
  function readJSON(id) {
    var el = document.getElementById(id);
    return el ? JSON.parse(el.textContent || "null") : null;
  }
  var META = readJSON("filter-meta");
  var ORDER = readJSON("filter-order");
  var CURRENT = readJSON("filter-conditions") || [];
  var container = document.getElementById("conditions");
  var addBtn = document.getElementById("add-condition");
  if (!META || !ORDER || !ORDER.length || !container || !addBtn) return;

  function option(value, label, selected) {
    var o = document.createElement("option");
    o.value = value;
    o.textContent = label;
    if (selected) o.selected = true;
    return o;
  }

  function valueControl(col, op, value) {
    var meta = META[col] || {};
    var opDef = (meta.ops || []).filter(function (o) { return o[0] === op; })[0];
    var needsValue = opDef ? opDef[2] : true;
    if (!needsValue) {
      var h = document.createElement("input");
      h.type = "hidden"; h.name = "fval"; h.value = "";
      return h;
    }
    if (meta.kind === "relation" || meta.kind === "enum") {
      var sel = document.createElement("select");
      sel.name = "fval"; sel.className = "fval";
      sel.appendChild(option("", "— choose —", false));
      (meta.choices || []).forEach(function (c) {
        sel.appendChild(option(String(c[0]), c[1], String(c[0]) === String(value)));
      });
      return sel;
    }
    var inp = document.createElement("input");
    inp.name = "fval"; inp.className = "fval";
    if (meta.kind === "number") { inp.type = "number"; inp.step = "any"; }
    else if (meta.data_type === "date") inp.type = "date";
    else if (meta.data_type === "datetime") inp.type = "datetime-local";
    else if (meta.data_type === "time") inp.type = "time";
    else inp.type = "text";
    if (value != null) inp.value = value;
    return inp;
  }

  function makeRow(cond) {
    cond = cond || {};
    var col = cond.col && META[cond.col] ? cond.col : ORDER[0];

    var colSel = document.createElement("select");
    colSel.name = "fcol"; colSel.className = "fcol";
    ORDER.forEach(function (c) { colSel.appendChild(option(c, META[c].label, c === col)); });

    var opSel = document.createElement("select");
    opSel.name = "fop"; opSel.className = "fop";
    function fillOps(selected) {
      opSel.innerHTML = "";
      (META[col].ops || []).forEach(function (o) {
        opSel.appendChild(option(o[0], o[1], o[0] === selected));
      });
    }
    fillOps(cond.op);

    var valWrap = document.createElement("span");
    valWrap.className = "fval-wrap";
    valWrap.appendChild(valueControl(col, opSel.value, cond.val));
    function rebuildValue() {
      valWrap.innerHTML = "";
      valWrap.appendChild(valueControl(col, opSel.value, ""));
    }

    colSel.addEventListener("change", function () {
      col = colSel.value; fillOps(null); rebuildValue();
    });
    opSel.addEventListener("change", rebuildValue);

    var rm = document.createElement("button");
    rm.type = "button"; rm.className = "btn sm"; rm.textContent = "✕";

    var row = document.createElement("div");
    row.className = "filter-row";
    rm.addEventListener("click", function () { row.remove(); });
    [colSel, opSel, valWrap, rm].forEach(function (n) { row.appendChild(n); });
    return row;
  }

  CURRENT.forEach(function (c) { container.appendChild(makeRow(c)); });
  addBtn.addEventListener("click", function () { container.appendChild(makeRow(null)); });
})();
