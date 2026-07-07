// Global keyboard shortcuts. They never fire while typing in a field (except
// Escape) or when a modifier key is held.
//   /   focus the global search box
//   n   go to the page's primary "new" action (a[data-sc="new"])
//   ?   toggle the help overlay      Esc  close it
(function () {
  "use strict";

  function typing(el) {
    if (!el) return false;
    var t = el.tagName;
    return t === "INPUT" || t === "TEXTAREA" || t === "SELECT" || el.isContentEditable;
  }

  var overlay = null;
  function build() {
    var d = document.createElement("div");
    d.id = "sc-help";
    d.setAttribute("role", "dialog");
    d.setAttribute("aria-modal", "true");
    d.setAttribute("aria-label", "Keyboard shortcuts");
    d.innerHTML =
      '<div class="sc-card"><h2 style="margin-top:0">Keyboard shortcuts</h2><table>' +
      '<tr><td><kbd>Ctrl</kbd>+<kbd>K</kbd></td><td>Command palette</td></tr>' +
      '<tr><td><kbd>/</kbd></td><td>Focus search</td></tr>' +
      '<tr><td><kbd>n</kbd></td><td>New record</td></tr>' +
      '<tr><td><kbd>?</kbd></td><td>Show this help</td></tr>' +
      '<tr><td><kbd>Esc</kbd></td><td>Close</td></tr>' +
      '</table><p class="muted">Press Esc to close</p></div>';
    d.addEventListener("click", function (e) { if (e.target === d) d.classList.remove("open"); });
    document.body.appendChild(d);
    return d;
  }
  function toggleHelp() {
    if (!overlay) overlay = build();
    overlay.classList.toggle("open");
  }

  document.addEventListener("keydown", function (e) {
    if (e.ctrlKey || e.metaKey || e.altKey) return;
    if (e.key === "Escape") { if (overlay) overlay.classList.remove("open"); return; }
    if (typing(e.target)) return;

    if (e.key === "/") {
      var s = document.querySelector('input[type="search"]');
      if (s) { e.preventDefault(); s.focus(); }
    } else if (e.key === "n") {
      var add = document.querySelector('a[data-sc="new"]');
      if (add) { e.preventDefault(); window.location.href = add.href; }
    } else if (e.key === "?") {
      e.preventDefault(); toggleHelp();
    }
  });
})();
