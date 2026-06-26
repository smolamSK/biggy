// Cascading dropdowns: a select with data-parent-field shows only the options whose
// data-parent matches the value of the controlling field. The empty option always shows;
// when nothing is selected in the controlling field, all options show.
(function () {
  document.addEventListener("DOMContentLoaded", function () {
    document.querySelectorAll("select[data-parent-field]").forEach(function (sel) {
      var parent = document.querySelector('[name="' + sel.getAttribute("data-parent-field") + '"]');
      if (!parent) return;
      function apply() {
        var pv = parent.value;
        for (var i = 0; i < sel.options.length; i++) {
          var opt = sel.options[i];
          var dp = opt.getAttribute("data-parent");
          var show = dp === null || dp === "" || pv === "" || dp === pv;
          opt.hidden = !show;
          opt.disabled = !show;
          if (!show && opt.selected) sel.value = "";
        }
      }
      parent.addEventListener("change", apply);
      apply();
    });
  });
})();
