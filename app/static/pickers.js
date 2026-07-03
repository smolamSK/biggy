// Type-to-filter pickers: progressively enhance relation <select data-picker>
// controls. The original select stays in the DOM (hidden) as the submitted
// control, so validation and no-JS behavior are unchanged. Single selects get
// a combobox (input + filtered list, arrows/Enter/Esc); multi selects get a
// filterable checkbox list with removable chips. Vanilla; no dependencies.
(function () {
  "use strict";
  var MIN_OPTIONS = 11;      // below this a plain select is easier

  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    return n;
  }

  // ---- single-select combobox ---------------------------------------------
  function comboify(sel) {
    var wrap = el("div", "picker");
    var input = el("input", "picker-input");
    input.type = "text";
    input.autocomplete = "off";
    input.placeholder = "Type to search…";
    var list = el("div", "picker-list");
    list.hidden = true;
    sel.parentNode.insertBefore(wrap, sel);
    wrap.appendChild(input);
    wrap.appendChild(list);
    wrap.appendChild(sel);
    sel.classList.add("picker-native");
    sel.tabIndex = -1;

    function options() {
      return Array.prototype.slice.call(sel.options);
    }
    function label() {
      var o = sel.options[sel.selectedIndex];
      return o ? o.textContent : "";
    }
    function render(q) {
      list.textContent = "";
      var ql = (q || "").toLowerCase();
      var shown = 0;
      options().forEach(function (o) {
        if (ql && o.textContent.toLowerCase().indexOf(ql) === -1) return;
        if (shown >= 50) return;               // keep the dropdown light
        shown += 1;
        var row = el("div", "picker-item" + (o.selected ? " selected" : ""), o.textContent);
        row.addEventListener("mousedown", function (e) {
          e.preventDefault();                   // keep focus
          sel.value = o.value;
          sel.dispatchEvent(new Event("change", { bubbles: true }));
          input.value = label();
          close();
        });
        list.appendChild(row);
      });
      if (!shown) list.appendChild(el("div", "picker-empty", "No matches"));
      list.hidden = false;
    }
    function close() { list.hidden = true; }
    function active() { return list.querySelector(".picker-item.active"); }

    input.value = label();
    input.addEventListener("focus", function () { input.select(); render(""); });
    input.addEventListener("input", function () { render(input.value); });
    input.addEventListener("blur", function () {
      setTimeout(function () { input.value = label(); close(); }, 120);
    });
    input.addEventListener("keydown", function (e) {
      if (list.hidden && (e.key === "ArrowDown" || e.key === "ArrowUp")) {
        render(input.value); e.preventDefault(); return;
      }
      if (e.key === "Escape") { input.value = label(); close(); return; }
      if (e.key === "ArrowDown" || e.key === "ArrowUp") {
        e.preventDefault();
        var items = Array.prototype.slice.call(list.querySelectorAll(".picker-item"));
        if (!items.length) return;
        var idx = items.indexOf(active());
        if (idx >= 0) items[idx].classList.remove("active");
        idx = e.key === "ArrowDown" ? Math.min(idx + 1, items.length - 1) : Math.max(idx - 1, 0);
        items[idx].classList.add("active");
        items[idx].scrollIntoView({ block: "nearest" });
      } else if (e.key === "Enter") {
        var a = active();
        if (a && !list.hidden) {
          e.preventDefault();
          a.dispatchEvent(new MouseEvent("mousedown"));
        }
      }
    });
  }

  // ---- multi-select: filterable checkbox list + chips ----------------------
  function multify(sel) {
    var wrap = el("div", "picker picker-multi");
    var chips = el("div", "picker-chips");
    var input = el("input", "picker-input");
    input.type = "text";
    input.autocomplete = "off";
    input.placeholder = "Type to filter…";
    var list = el("div", "picker-list picker-checklist");
    sel.parentNode.insertBefore(wrap, sel);
    wrap.appendChild(chips);
    wrap.appendChild(input);
    wrap.appendChild(list);
    wrap.appendChild(sel);
    sel.classList.add("picker-native");
    sel.tabIndex = -1;

    function renderChips() {
      chips.textContent = "";
      Array.prototype.forEach.call(sel.selectedOptions, function (o) {
        var c = el("span", "picker-chip", o.textContent + " ");
        var x = el("button", "picker-chip-x", "×");
        x.type = "button";
        x.title = "Remove";
        x.addEventListener("click", function () {
          o.selected = false;
          sel.dispatchEvent(new Event("change", { bubbles: true }));
          renderChips(); renderList(input.value);
        });
        c.appendChild(x);
        chips.appendChild(c);
      });
    }
    function renderList(q) {
      list.textContent = "";
      var ql = (q || "").toLowerCase();
      var shown = 0;
      Array.prototype.forEach.call(sel.options, function (o) {
        if (ql && o.textContent.toLowerCase().indexOf(ql) === -1) return;
        if (shown >= 50) return;
        shown += 1;
        var row = el("label", "picker-check");
        var cb = el("input");
        cb.type = "checkbox";
        cb.checked = o.selected;
        cb.addEventListener("change", function () {
          o.selected = cb.checked;
          sel.dispatchEvent(new Event("change", { bubbles: true }));
          renderChips();
        });
        row.appendChild(cb);
        row.appendChild(document.createTextNode(" " + o.textContent));
        list.appendChild(row);
      });
      if (!shown) list.appendChild(el("div", "picker-empty", "No matches"));
    }
    input.addEventListener("input", function () { renderList(input.value); });
    renderChips();
    renderList("");
  }

  document.addEventListener("DOMContentLoaded", function () {
    document.querySelectorAll("select[data-picker]").forEach(function (sel) {
      if (sel.disabled || sel.options.length < MIN_OPTIONS) return;
      if (sel.multiple) multify(sel); else comboify(sel);
    });
  });
})();
