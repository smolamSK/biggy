// Interactive ER diagram: table boxes + relation edges with zoom / pan / drag.
// Layout and view persist in localStorage. Vanilla; no dependencies.
(function () {
  var SVGNS = "http://www.w3.org/2000/svg";
  var KEY = "biggy.diagram";

  document.addEventListener("DOMContentLoaded", function () {
    var node = document.getElementById("er-graph");
    var canvas = document.getElementById("er-canvas");
    var world = document.getElementById("er-world");
    var svg = document.getElementById("er-edges");
    if (!node || !canvas || !world || !svg) return;
    var graph = JSON.parse(node.textContent || "{}");
    if (!graph.tables || !graph.tables.length) return;

    var boxes = {};            // tableId -> DOM box
    var positions = {};        // tableId -> {x, y}
    var view = { x: 20, y: 20, scale: 1 };

    function el(tag, attrs, kids) {
      var n = document.createElement(tag);
      attrs = attrs || {};
      Object.keys(attrs).forEach(function (k) {
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

    // --- persistence ----------------------------------------------------
    function read() {
      try { return JSON.parse(localStorage.getItem(KEY)) || {}; } catch (e) { return {}; }
    }
    function save() {
      try { localStorage.setItem(KEY, JSON.stringify({ positions: positions, view: view })); }
      catch (e) { /* ignore */ }
    }

    function gridLayout() {
      var pos = {}, cols = Math.ceil(Math.sqrt(graph.tables.length));
      graph.tables.forEach(function (t, i) {
        pos[t.id] = { x: (i % cols) * 300 + 40, y: Math.floor(i / cols) * 280 + 40 };
      });
      return pos;
    }

    // --- rendering ------------------------------------------------------
    function buildBox(t) {
      var head = el("div", { class: "er-head" });
      head.appendChild(el("span", { class: "er-title" }, [document.createTextNode(t.label)]));
      var open = el("a", { class: "er-open", href: t.url, title: "Open table" });
      open.textContent = "↗";
      head.appendChild(open);
      var box = el("div", { class: "er-table", "data-id": t.id }, [head]);
      t.fields.forEach(function (f) {
        var name = el("span", { class: "er-name" }, [document.createTextNode(f.name)]);
        if (f.pk) { var pk = el("span", { class: "er-pk" }); pk.textContent = "PK"; name.appendChild(pk); }
        if (f.fk_to) { var fk = el("span", { class: "er-fk" }); fk.textContent = "→"; name.appendChild(fk); }
        box.appendChild(el("div", { class: "er-row" },
          [name, el("span", { class: "er-type" }, [document.createTextNode(f.type)])]));
      });
      head.addEventListener("pointerdown", function (e) {
        if (e.target.closest(".er-open")) return;   // let the open link work
        e.stopPropagation();
        e.preventDefault();
        startDrag("box", e, { id: t.id, ox: positions[t.id].x, oy: positions[t.id].y });
      });
      return box;
    }

    function positionBox(id) {
      boxes[id].style.left = positions[id].x + "px";
      boxes[id].style.top = positions[id].y + "px";
    }
    function applyView() {
      world.style.transform = "translate(" + view.x + "px," + view.y + "px) scale(" + view.scale + ")";
    }
    function rectOf(id) {
      var b = boxes[id], p = positions[id];
      return { x: p.x, y: p.y, w: b.offsetWidth, h: b.offsetHeight };
    }
    function borderPoint(r, tx, ty) {
      var cx = r.x + r.w / 2, cy = r.y + r.h / 2, dx = tx - cx, dy = ty - cy;
      if (!dx && !dy) return { x: cx, y: cy };
      var sx = dx ? (r.w / 2) / Math.abs(dx) : Infinity;
      var sy = dy ? (r.h / 2) / Math.abs(dy) : Infinity;
      var s = Math.min(sx, sy);
      return { x: cx + dx * s, y: cy + dy * s };
    }

    function drawEdges() {
      var maxX = 0, maxY = 0;
      graph.tables.forEach(function (t) {
        var r = rectOf(t.id); maxX = Math.max(maxX, r.x + r.w); maxY = Math.max(maxY, r.y + r.h);
      });
      svg.setAttribute("width", maxX + 80);
      svg.setAttribute("height", maxY + 80);
      while (svg.firstChild) svg.removeChild(svg.firstChild);

      var defs = svgEl("defs", {});
      var marker = svgEl("marker", { id: "er-arrow", viewBox: "0 0 10 10", refX: "9", refY: "5",
        markerWidth: "7", markerHeight: "7", orient: "auto-start-reverse" });
      marker.appendChild(svgEl("path", { d: "M0,0 L10,5 L0,10 z", fill: "#64748b" }));
      defs.appendChild(marker);
      svg.appendChild(defs);

      graph.relations.forEach(function (rel) {
        if (!boxes[rel.from] || !boxes[rel.to]) return;
        var cls = "er-edge" + (rel.kind === "mn" ? " mn" : "");
        if (rel.from === rel.to) {                       // self relation: small loop
          var r = rectOf(rel.from), x = r.x + r.w, y = r.y + r.h / 2;
          var loop = svgEl("path", { d: "M" + x + "," + (y - 12) + " c 34,-16 34,40 0,24", class: cls });
          if (rel.kind === "m1") loop.setAttribute("marker-end", "url(#er-arrow)");
          svg.appendChild(loop);
          return;
        }
        var ra = rectOf(rel.from), rb = rectOf(rel.to);
        var ca = { x: ra.x + ra.w / 2, y: ra.y + ra.h / 2 };
        var cb = { x: rb.x + rb.w / 2, y: rb.y + rb.h / 2 };
        var pa = borderPoint(ra, cb.x, cb.y), pb = borderPoint(rb, ca.x, ca.y);
        var line = svgEl("line", { x1: pa.x, y1: pa.y, x2: pb.x, y2: pb.y, class: cls });
        if (rel.kind === "m1") line.setAttribute("marker-end", "url(#er-arrow)");
        svg.appendChild(line);
        if (rel.kind === "mn" && rel.label) {
          var label = svgEl("text", { x: (pa.x + pb.x) / 2, y: (pa.y + pb.y) / 2 - 4, class: "er-edge-label" });
          label.textContent = rel.label;
          svg.appendChild(label);
        }
      });
    }

    // --- interaction ----------------------------------------------------
    var drag = null;
    function startDrag(type, e, extra) {
      drag = Object.assign({ type: type, sx: e.clientX, sy: e.clientY }, extra || {});
    }
    canvas.addEventListener("pointerdown", function (e) {
      if (e.target.closest(".er-table")) return;       // box body / head handled elsewhere
      startDrag("pan", e, { ox: view.x, oy: view.y });
      canvas.classList.add("grabbing");
    });
    document.addEventListener("pointermove", function (e) {
      if (!drag) return;
      var dx = e.clientX - drag.sx, dy = e.clientY - drag.sy;
      if (drag.type === "pan") {
        view.x = drag.ox + dx; view.y = drag.oy + dy; applyView();
      } else {
        positions[drag.id].x = drag.ox + dx / view.scale;
        positions[drag.id].y = drag.oy + dy / view.scale;
        positionBox(drag.id); drawEdges();
      }
    });
    document.addEventListener("pointerup", function () {
      if (!drag) return;
      canvas.classList.remove("grabbing");
      drag = null; save();
    });

    function zoomAt(mx, my, factor) {
      var ns = clamp(view.scale * factor, 0.2, 2.5);
      factor = ns / view.scale;
      view.x = mx - (mx - view.x) * factor;
      view.y = my - (my - view.y) * factor;
      view.scale = ns;
      applyView(); save();
    }
    canvas.addEventListener("wheel", function (e) {
      e.preventDefault();
      var r = canvas.getBoundingClientRect();
      zoomAt(e.clientX - r.left, e.clientY - r.top, e.deltaY < 0 ? 1.1 : 1 / 1.1);
    }, { passive: false });

    function zoomCenter(factor) {
      var r = canvas.getBoundingClientRect();
      zoomAt(r.width / 2, r.height / 2, factor);
    }
    function fit() {
      var minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
      graph.tables.forEach(function (t) {
        var r = rectOf(t.id);
        minX = Math.min(minX, r.x); minY = Math.min(minY, r.y);
        maxX = Math.max(maxX, r.x + r.w); maxY = Math.max(maxY, r.y + r.h);
      });
      var box = canvas.getBoundingClientRect(), pad = 40;
      var sw = (maxX - minX) + pad * 2, sh = (maxY - minY) + pad * 2;
      view.scale = clamp(Math.min(box.width / sw, box.height / sh), 0.2, 1.5);
      view.x = (box.width - (maxX - minX) * view.scale) / 2 - minX * view.scale;
      view.y = (box.height - (maxY - minY) * view.scale) / 2 - minY * view.scale;
      applyView(); save();
    }
    function resetLayout() {
      positions = gridLayout();
      graph.tables.forEach(function (t) { positionBox(t.id); });
      drawEdges(); fit();
    }

    var byId = { "er-zoom-in": function () { zoomCenter(1.2); },
                 "er-zoom-out": function () { zoomCenter(1 / 1.2); },
                 "er-fit": fit, "er-reset": resetLayout };
    Object.keys(byId).forEach(function (id) {
      var b = document.getElementById(id);
      if (b) b.addEventListener("click", byId[id]);
    });

    // --- init -----------------------------------------------------------
    var saved = read();
    positions = gridLayout();
    if (saved.positions) {
      graph.tables.forEach(function (t) {
        if (saved.positions[t.id]) positions[t.id] = saved.positions[t.id];
      });
    }
    graph.tables.forEach(function (t) {
      var b = buildBox(t); boxes[t.id] = b; world.appendChild(b); positionBox(t.id);
    });
    drawEdges();
    if (saved.view) { view = saved.view; applyView(); } else { fit(); }
  });
})();
