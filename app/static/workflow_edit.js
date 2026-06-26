// Visual status-workflow editor: draggable state nodes + directed transition
// edges. Click a state then another to toggle a transition; click an arrow to
// select it (edit roles / delete); "Set initial" marks the selected state.
// Adapts the ER-diagram canvas (diagram.js). Vanilla; no dependencies.
(function () {
  "use strict";
  var SVGNS = "http://www.w3.org/2000/svg";

  document.addEventListener("DOMContentLoaded", function () {
    var node = document.getElementById("wf-graph");
    var canvas = document.getElementById("er-canvas");
    var world = document.getElementById("er-world");
    var svg = document.getElementById("er-edges");
    if (!node || !canvas || !world || !svg) return;
    var graph = JSON.parse(node.textContent || "{}");
    var states = graph.states || [];

    var transitions = (graph.transitions || []).map(function (t) {
      return { from: t.from, to: t.to, roles: (t.roles || []).slice() };
    });
    var layout = graph.layout || {};
    var initial = graph.initial || null;
    var roles = graph.roles || [];

    var nodes = {};                 // state -> DOM box
    var positions = {};             // state -> {x,y}
    var view = { x: 20, y: 20, scale: 1 };
    var selectedNode = null;        // for connect + set-initial
    var pendingFrom = null;         // first node of a pending transition
    var selectedEdge = null;        // index into transitions

    function el(tag, attrs, kids) {
      var n = document.createElement(tag);
      Object.keys(attrs || {}).forEach(function (k) {
        if (k === "class") n.className = attrs[k]; else n.setAttribute(k, attrs[k]);
      });
      (kids || []).forEach(function (c) { n.appendChild(c); });
      return n;
    }
    function svgEl(tag, attrs) {
      var n = document.createElementNS(SVGNS, tag);
      Object.keys(attrs || {}).forEach(function (k) { n.setAttribute(k, attrs[k]); });
      return n;
    }
    function clamp(v, lo, hi) { return Math.max(lo, Math.min(hi, v)); }
    function status(msg) { document.getElementById("wf-status").textContent = msg || ""; }

    function gridLayout() {
      var cols = Math.ceil(Math.sqrt(states.length)) || 1;
      states.forEach(function (s, i) {
        if (!positions[s]) positions[s] = { x: (i % cols) * 200 + 40, y: Math.floor(i / cols) * 140 + 40 };
      });
    }

    function buildNode(s) {
      var box = el("div", { class: "wf-node", "data-state": s },
        [el("span", { class: "wf-name" }, [document.createTextNode(s)])]);
      box.addEventListener("pointerdown", function (e) {
        e.stopPropagation();
        startDrag("node", e, { id: s, ox: positions[s].x, oy: positions[s].y, moved: false });
      });
      return box;
    }
    function positionNode(s) {
      nodes[s].style.left = positions[s].x + "px";
      nodes[s].style.top = positions[s].y + "px";
    }
    function applyView() {
      world.style.transform = "translate(" + view.x + "px," + view.y + "px) scale(" + view.scale + ")";
    }
    function rectOf(s) {
      var b = nodes[s], p = positions[s];
      return { x: p.x, y: p.y, w: b.offsetWidth, h: b.offsetHeight };
    }
    function borderPoint(r, tx, ty) {
      var cx = r.x + r.w / 2, cy = r.y + r.h / 2, dx = tx - cx, dy = ty - cy;
      if (!dx && !dy) return { x: cx, y: cy };
      var s = Math.min(dx ? (r.w / 2) / Math.abs(dx) : Infinity,
                       dy ? (r.h / 2) / Math.abs(dy) : Infinity);
      return { x: cx + dx * s, y: cy + dy * s };
    }

    function refreshNodeClasses() {
      Object.keys(nodes).forEach(function (s) {
        nodes[s].classList.toggle("sel", s === selectedNode);
        nodes[s].classList.toggle("pending", s === pendingFrom);
        nodes[s].classList.toggle("initial", s === initial);
      });
    }

    function drawEdges() {
      var maxX = 0, maxY = 0;
      states.forEach(function (s) {
        var r = rectOf(s); maxX = Math.max(maxX, r.x + r.w); maxY = Math.max(maxY, r.y + r.h);
      });
      svg.setAttribute("width", maxX + 120);
      svg.setAttribute("height", maxY + 120);
      while (svg.firstChild) svg.removeChild(svg.firstChild);
      var defs = svgEl("defs", {});
      var marker = svgEl("marker", { id: "wf-arrow", viewBox: "0 0 10 10", refX: "9", refY: "5",
        markerWidth: "8", markerHeight: "8", orient: "auto-start-reverse" });
      marker.appendChild(svgEl("path", { d: "M0,0 L10,5 L0,10 z", fill: "#64748b" }));
      defs.appendChild(marker);
      svg.appendChild(defs);

      transitions.forEach(function (t, i) {
        if (!nodes[t.from] || !nodes[t.to] || t.from === t.to) return;
        var ra = rectOf(t.from), rb = rectOf(t.to);
        var ca = { x: ra.x + ra.w / 2, y: ra.y + ra.h / 2 };
        var cb = { x: rb.x + rb.w / 2, y: rb.y + rb.h / 2 };
        var pa = borderPoint(ra, cb.x, cb.y), pb = borderPoint(rb, ca.x, ca.y);
        // Bow an edge only when its reverse also exists, so the two arrows don't
        // overlap. The normal (nx,ny) already flips with travel direction, so a
        // CONSTANT offset sign puts A->B and B->A on opposite sides; a lone edge
        // (off = 0) stays straight.
        var mx = (pa.x + pb.x) / 2, my = (pa.y + pb.y) / 2;
        var nx = -(pb.y - pa.y), ny = (pb.x - pa.x);
        var len = Math.hypot(nx, ny) || 1;
        var off = findEdge(t.to, t.from) >= 0 ? 26 : 0;
        var qx = mx + nx / len * off, qy = my + ny / len * off;
        var d = "M" + pa.x + "," + pa.y + " Q" + qx + "," + qy + " " + pb.x + "," + pb.y;
        var path = svgEl("path", {
          d: d, class: "wf-edge" + (i === selectedEdge ? " sel" : ""),
          "data-index": i, "marker-end": "url(#wf-arrow)", fill: "none",
        });
        svg.appendChild(path);
        // wide, invisible hit area so clicking anywhere on the arrow opens its
        // rights (a 1.5px stroke is otherwise nearly impossible to click).
        var hit = svgEl("path", { d: d, class: "wf-hit", "data-index": i });
        hit.addEventListener("pointerdown", function (e) {
          e.stopPropagation(); selectEdge(i);
        });
        svg.appendChild(hit);
        if (t.roles && t.roles.length) {
          var label = svgEl("text", { x: qx, y: qy - 4, class: "wf-edge-label" });
          label.textContent = t.roles.join(", ");
          svg.appendChild(label);
        }
      });
    }

    function findEdge(from, to) {
      for (var i = 0; i < transitions.length; i++) {
        if (transitions[i].from === from && transitions[i].to === to) return i;
      }
      return -1;
    }
    function toggleTransition(from, to) {
      if (from === to) return;
      var i = findEdge(from, to);
      if (i >= 0) { transitions.splice(i, 1); selectedEdge = null; }
      else { transitions.push({ from: from, to: to, roles: [] }); selectedEdge = transitions.length - 1; }
      drawEdges(); showRoles();
    }

    function selectNode(s) {
      selectedEdge = null;
      if (pendingFrom && pendingFrom !== s) {
        toggleTransition(pendingFrom, s);
        pendingFrom = null; selectedNode = s;
      } else if (pendingFrom === s) {
        pendingFrom = null;
      } else {
        pendingFrom = s; selectedNode = s;
      }
      refreshNodeClasses(); showRoles();
    }
    function selectEdge(i) {
      selectedEdge = i; pendingFrom = null; selectedNode = null;
      refreshNodeClasses(); drawEdges(); showRoles();
    }

    function showRoles() {
      var panel = document.getElementById("wf-roles");
      var boxes = document.getElementById("wf-roles-boxes");
      if (selectedEdge === null || !transitions[selectedEdge]) { panel.hidden = true; return; }
      panel.hidden = false;
      panel.scrollIntoView({ block: "nearest" });   // make the rights editor visible
      boxes.innerHTML = "";
      var t = transitions[selectedEdge];
      var title = el("div", { class: "muted" });
      title.textContent = t.from + " → " + t.to;
      boxes.appendChild(title);
      roles.forEach(function (r) {
        var cb = el("input", { type: "checkbox" });
        cb.checked = t.roles.indexOf(r) !== -1;
        cb.addEventListener("change", function () {
          if (cb.checked) { if (t.roles.indexOf(r) === -1) t.roles.push(r); }
          else { t.roles = t.roles.filter(function (x) { return x !== r; }); }
          drawEdges();
        });
        boxes.appendChild(el("label", { class: "checkline",
          style: "border:1px solid var(--line);border-radius:7px;padding:.2rem .5rem" },
          [cb, document.createTextNode(" " + r)]));
      });
    }

    // --- interaction (pan / drag / click) -------------------------------
    var drag = null;
    function startDrag(type, e, extra) {
      drag = Object.assign({ type: type, sx: e.clientX, sy: e.clientY }, extra || {});
    }
    canvas.addEventListener("pointerdown", function (e) {
      if (e.target.closest(".wf-node") || e.target.closest(".wf-edge")) return;
      pendingFrom = null; selectedNode = null; selectedEdge = null;
      refreshNodeClasses(); drawEdges(); showRoles();
      startDrag("pan", e, { ox: view.x, oy: view.y });
      canvas.classList.add("grabbing");
    });
    document.addEventListener("pointermove", function (e) {
      if (!drag) return;
      var dx = e.clientX - drag.sx, dy = e.clientY - drag.sy;
      if (drag.type === "pan") {
        view.x = drag.ox + dx; view.y = drag.oy + dy; applyView();
      } else if (drag.type === "node") {
        if (Math.abs(dx) + Math.abs(dy) > 3) drag.moved = true;
        positions[drag.id].x = drag.ox + dx / view.scale;
        positions[drag.id].y = drag.oy + dy / view.scale;
        positionNode(drag.id); drawEdges();
      }
    });
    document.addEventListener("pointerup", function () {
      if (!drag) return;
      canvas.classList.remove("grabbing");
      if (drag.type === "node" && !drag.moved) selectNode(drag.id);
      drag = null;
    });
    canvas.addEventListener("wheel", function (e) {
      e.preventDefault();
      var r = canvas.getBoundingClientRect(), mx = e.clientX - r.left, my = e.clientY - r.top;
      var ns = clamp(view.scale * (e.deltaY < 0 ? 1.1 : 1 / 1.1), 0.3, 2.5), f = ns / view.scale;
      view.x = mx - (mx - view.x) * f; view.y = my - (my - view.y) * f; view.scale = ns;
      applyView();
    }, { passive: false });

    document.getElementById("wf-initial").addEventListener("click", function () {
      if (!selectedNode) { status("Select a state first."); return; }
      initial = selectedNode; refreshNodeClasses(); status("Initial = " + initial);
    });
    document.getElementById("wf-del-edge").addEventListener("click", function () {
      if (selectedEdge === null) { status("Select a transition (click its arrow) first."); return; }
      transitions.splice(selectedEdge, 1); selectedEdge = null; drawEdges(); showRoles();
    });
    document.getElementById("wf-save").addEventListener("click", function () {
      var token = document.getElementById("wf-csrf").value;
      fetch(graph.save_url, {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-CSRFToken": token },
        body: JSON.stringify({ transitions: transitions, layout: positions, initial: initial }),
      }).then(function (r) { return r.json().then(function (d) { return { ok: r.ok, d: d }; }); })
        .then(function (res) { status(res.ok && res.d.ok ? "Saved." : "Save failed."); })
        .catch(function () { status("Save failed (network)."); });
    });

    // --- init -----------------------------------------------------------
    gridLayout();
    states.forEach(function (s) {
      if (layout[s]) positions[s] = { x: layout[s].x, y: layout[s].y };
    });
    gridLayout();   // fill any without a saved position
    states.forEach(function (s) {
      var b = buildNode(s); nodes[s] = b; world.appendChild(b); positionNode(s);
    });
    refreshNodeClasses();
    applyView();
    drawEdges();
  });
})();
