// Theme picker: sync the header <select> with the active theme and persist the
// choice. The theme attribute itself is applied pre-paint by an inline script in
// the page <head> (so there is no flash of the wrong theme on load).
(function () {
  "use strict";
  var sel = document.getElementById("theme-select");
  if (!sel) return;
  sel.value = document.documentElement.getAttribute("data-theme") || "light";
  sel.addEventListener("change", function () {
    document.documentElement.setAttribute("data-theme", sel.value);
    try { localStorage.setItem("biggy.theme", sel.value); } catch (e) {}
  });
})();
