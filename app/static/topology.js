// Dependency / impact map: node-link graph of a CI and the records it depends
// on (upstream) or that depend on it (downstream). Data is the JSON in
// #topo-graph (built by app/topology.py). Concentric layout by BFS depth, with
// pan / zoom / drag — same interaction model as the schema diagram. Vanilla, no
// dependencies. Layout/view persist in localStorage per root record.
(function () {
  "use strict";
  var SVGNS = "http://www.w3.org/2000/svg";
  // depth -> node tint (root, ring 1, ring 2, ...)
  var TINTS = ["#2563eb", "#0e7490", "#16a34a", "#d97706", "#7c3aed", "#db2777"];

  document.addEventListener("DOMContentLoaded", function () {
    var dataNode = document.getElementById("topo-graph");
    var canvas = document.getElementById("topo-canvas");
    var world = document.getElementById("topo-world");
    var svg = document.getElementById("topo-edges");
    if (!dataNode || !canvas || !world || !svg) return;
    var graph = JSON.parse(dataNode.textContent || "{}");
    if (!graph.nodes || !graph.nodes.length) return;

    var KEY = "biggy.topology." + graph.root;
    var boxes = {};            // node id -> DOM box
    var positions = {};        // node id -> {x, y}  (box top-left in world coords)
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
    function tint(depth) { return TINTS[Math.min(depth, TINTS.length - 1)]; }

    // --- persistence ----------------------------------------------------
    function read() {
      try { return JSON.parse(localStorage.getItem(KEY)) || {}; } catch (e) { return {}; }
    }
    function save() {
      try { localStorage.setItem(KEY, JSON.stringify({ positions: positions, view: view })); }
      catch (e) { /* ignore */ }
    }

    // --- layout: concentric rings by BFS depth --------------------------
    function ringCenters() {
      var CX = 660, CY = 430, RING = 240, centers = {}, byDepth = {};
      graph.nodes.forEach(function (n) { (byDepth[n.depth] = byDepth[n.depth] || []).push(n); });
      Object.keys(byDepth).forEach(function (dk) {
        var d = +dk, ring = byDepth[dk];
        if (d === 0) { ring.forEach(function (n) { centers[n.id] = { x: CX, y: CY }; }); return; }
        var r = d * RING, off = (d % 2) * 0.35;   // offset alternate rings so they don't align
        ring.forEach(function (n, i) {
          var a = (i / ring.length) * 2 * Math.PI - Math.PI / 2 + off;
          centers[n.id] = { x: CX + r * Math.cos(a), y: CY + r * Math.sin(a) };
        });
      });
      return centers;
    }

    // --- rendering ------------------------------------------------------
    function buildBox(n) {
      var head = el("div", { class: "topo-head", style: "background:" + tint(n.depth) });
      var title = el("a", { class: "topo-title", href: n.topo_url, title: "Recenter the map on this CI" },
                     [document.createTextNode(n.label)]);
      head.appendChild(title);
      var open = el("a", { class: "topo-open", href: n.url, title: "Open record" });
      open.textContent = "↗";
      head.appendChild(open);
      var box = el("div", { class: "topo-node" + (n.depth === 0 ? " root" : ""), "data-id": n.id }, [head]);
      box.appendChild(el("div", { class: "topo-type" }, [document.createTextNode(n.table_label)]));
      head.addEventListener("pointerdown", function (e) {
        if (e.target.closest("a")) return;            // let title/open links work
        e.stopPropagation(); e.preventDefault();
        startDrag("box", e, { id: n.id, ox: positions[n.id].x, oy: positions[n.id].y });
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
      graph.nodes.forEach(function (n) {
        var r = rectOf(n.id); maxX = Math.max(maxX, r.x + r.w); maxY = Math.max(maxY, r.y + r.h);
      });
      svg.setAttribute("width", maxX + 80);
      svg.setAttribute("height", maxY + 80);
      while (svg.firstChild) svg.removeChild(svg.firstChild);

      var defs = svgEl("defs", {});
      var marker = svgEl("marker", { id: "topo-arrow", viewBox: "0 0 10 10", refX: "9", refY: "5",
        markerWidth: "7", markerHeight: "7", orient: "auto-start-reverse" });
      marker.appendChild(svgEl("path", { d: "M0,0 L10,5 L0,10 z", fill: "#64748b" }));
      defs.appendChild(marker);
      svg.appendChild(defs);

      graph.edges.forEach(function (e) {
        if (!boxes[e.source] || !boxes[e.target] || e.source === e.target) return;
        var ra = rectOf(e.source), rb = rectOf(e.target);
        var ca = { x: ra.x + ra.w / 2, y: ra.y + ra.h / 2 };
        var cb = { x: rb.x + rb.w / 2, y: rb.y + rb.h / 2 };
        var pa = borderPoint(ra, cb.x, cb.y), pb = borderPoint(rb, ca.x, ca.y);
        var line = svgEl("line", { x1: pa.x, y1: pa.y, x2: pb.x, y2: pb.y,
                                   class: "topo-edge" + (e.kind === "mn" ? " mn" : "") });
        if (e.directed) line.setAttribute("marker-end", "url(#topo-arrow)");
        svg.appendChild(line);
      });
    }

    // --- interaction ----------------------------------------------------
    var drag = null;
    function startDrag(type, e, extra) {
      drag = Object.assign({ type: type, sx: e.clientX, sy: e.clientY }, extra || {});
    }
    canvas.addEventListener("pointerdown", function (e) {
      if (e.target.closest(".topo-node")) return;
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
      view.scale = ns; applyView(); save();
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
      graph.nodes.forEach(function (n) {
        var r = rectOf(n.id);
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

    var byId = { "topo-zoom-in": function () { zoomCenter(1.2); },
                 "topo-zoom-out": function () { zoomCenter(1 / 1.2); },
                 "topo-fit": fit };
    Object.keys(byId).forEach(function (id) {
      var b = document.getElementById(id);
      if (b) b.addEventListener("click", byId[id]);
    });

    // --- init -----------------------------------------------------------
    graph.nodes.forEach(function (n) {
      var b = buildBox(n); boxes[n.id] = b; world.appendChild(b);
    });
    var saved = read();
    var centers = ringCenters();
    graph.nodes.forEach(function (n) {
      if (saved.positions && saved.positions[n.id]) {
        positions[n.id] = saved.positions[n.id];
      } else {
        var c = centers[n.id], b = boxes[n.id];
        positions[n.id] = { x: c.x - b.offsetWidth / 2, y: c.y - b.offsetHeight / 2 };
      }
      positionBox(n.id);
    });
    drawEdges();
    if (saved.view) { view = saved.view; applyView(); } else { fit(); }
  });
})();
