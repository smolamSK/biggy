// Kanban drag/drop. Dropping a card in another column posts the new value to the
// cell endpoint (col = the board's group field) — which validates the value and
// enforces any status workflow. The card moves only after the server confirms.
(function () {
  "use strict";
  var board = document.querySelector(".kanban");
  if (!board) return;
  var csrf = (document.getElementById("kanban-csrf") || {}).value || "";
  var dragged = null;

  board.addEventListener("dragstart", function (e) {
    var card = e.target.closest(".kcard");
    if (!card) return;
    dragged = card;
    e.dataTransfer.effectAllowed = "move";
    try { e.dataTransfer.setData("text/plain", card.dataset.pk); } catch (x) {}
  });

  board.querySelectorAll(".kcol").forEach(function (col) {
    col.addEventListener("dragover", function (e) { e.preventDefault(); col.classList.add("drop"); });
    col.addEventListener("dragleave", function () { col.classList.remove("drop"); });
    col.addEventListener("drop", function (e) {
      e.preventDefault();
      col.classList.remove("drop");
      if (!dragged) return;
      var from = dragged.closest(".kcol");
      if (col === from) return;
      var card = dragged;
      var body = new URLSearchParams();
      body.set("csrf_token", csrf);
      body.set("col", card.dataset.col);
      body.set("value", col.dataset.value);
      fetch(card.dataset.url, { method: "POST", body: body })
        .then(function (r) { return r.json().then(function (d) { return { ok: r.ok, d: d }; }); })
        .then(function (res) {
          if (res.ok && res.d.ok) {
            col.querySelector(".kcards").appendChild(card);
            recount(from); recount(col);
          } else {
            alert((res.d && res.d.error) || "Move not allowed.");
          }
        })
        .catch(function () { alert("Move failed (network)."); });
    });
  });

  function recount(col) {
    var n = col.querySelectorAll(".kcard").length;
    var badge = col.querySelector(".kcol-count");
    if (badge) badge.textContent = n;
  }
})();
