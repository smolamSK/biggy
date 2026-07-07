// Minimal inline-SVG charts for report results / dashboard tiles. No library.
// Each `.js-chart` carries data-type (bar|line|pie) and an inner <script
// type="application/json"> with {grouped, labels, series:[{name, values}]}.
// Renders the FIRST metric series. Vanilla; themed via currentColor + a palette.
(function () {
  "use strict";
  var NS = "http://www.w3.org/2000/svg";
  var PAL = ["#2563eb", "#16a34a", "#d97706", "#dc2626", "#7c3aed",
             "#0e7490", "#db2777", "#65a30d"];
  var W = 640, H = 260, L = 44, R = 12, T = 18, B = 46;

  function svgEl(tag, attrs) {
    var n = document.createElementNS(NS, tag);
    Object.keys(attrs || {}).forEach(function (k) { n.setAttribute(k, attrs[k]); });
    return n;
  }
  function text(x, y, s, attrs) {
    var t = svgEl("text", Object.assign({ x: x, y: y }, attrs || {}));
    t.textContent = s;
    return t;
  }
  function clip(s, n) { s = String(s); return s.length > n ? s.slice(0, n - 1) + "…" : s; }
  function fmt(v) { return String(+v.toFixed(2)).replace(/\.00$/, ""); }
  function titled(node, s) {
    // native SVG tooltip: shown on hover, read by screen readers
    var t = svgEl("title");
    t.textContent = s;
    node.appendChild(t);
    return node;
  }

  function frame() {
    var svg = svgEl("svg", { viewBox: "0 0 " + W + " " + H, class: "chart-svg",
                             preserveAspectRatio: "xMidYMid meet" });
    svg.setAttribute("style", "width:100%;height:auto;max-height:300px");
    return svg;
  }

  function noData(el) {
    var p = document.createElement("p");
    p.className = "muted"; p.textContent = "No data to chart.";
    el.appendChild(p);
  }

  function barOrLine(el, type, labels, values, name) {
    var svg = frame();
    var pw = W - L - R, ph = H - T - B, n = values.length;
    var max = Math.max.apply(null, values.concat([1]));
    svg.appendChild(svgEl("line", { x1: L, y1: T + ph, x2: L + pw, y2: T + ph, stroke: "var(--line)" }));
    var pts = [];
    values.forEach(function (v, i) {
      var cx = L + (n === 1 ? pw / 2 : (type === "line" ? i * (pw / Math.max(1, n - 1)) : (i + 0.5) * (pw / n)));
      var h = (v / max) * ph, y = T + ph - h;
      if (type === "bar") {
        var bw = Math.min(48, (pw / n) * 0.7);
        svg.appendChild(titled(svgEl("rect", { x: cx - bw / 2, y: y, width: bw, height: h,
          fill: PAL[i % PAL.length], rx: 2 }), labels[i] + ": " + fmt(v)));
      }
      pts.push(cx + "," + y);
      svg.appendChild(text(cx, y - 4, fmt(v),
        { "text-anchor": "middle", class: "chart-val" }));
      svg.appendChild(text(cx, T + ph + 16, clip(labels[i], 10),
        { "text-anchor": "middle", class: "chart-lbl" }));
    });
    if (type === "line") {
      svg.appendChild(svgEl("polyline", { points: pts.join(" "), fill: "none",
        stroke: PAL[0], "stroke-width": 2 }));
      pts.forEach(function (p, i) {
        var xy = p.split(",");
        svg.appendChild(titled(svgEl("circle", { cx: xy[0], cy: xy[1], r: 3, fill: PAL[0] }),
          labels[i] + ": " + fmt(values[i])));
      });
    }
    svg.appendChild(text(L, T - 6, name, { class: "chart-lbl" }));
    el.appendChild(svg);
  }

  function pie(el, labels, values) {
    var total = values.reduce(function (a, b) { return a + b; }, 0);
    if (total <= 0) { noData(el); return; }
    var svg = frame();
    var cx = 130, cy = H / 2, r = 100, ang = -Math.PI / 2;
    values.forEach(function (v, i) {
      var a2 = ang + (v / total) * Math.PI * 2;
      var large = (a2 - ang) > Math.PI ? 1 : 0;
      var x1 = cx + r * Math.cos(ang), y1 = cy + r * Math.sin(ang);
      var x2 = cx + r * Math.cos(a2), y2 = cy + r * Math.sin(a2);
      svg.appendChild(titled(svgEl("path", {
        d: "M" + cx + "," + cy + " L" + x1 + "," + y1 +
           " A" + r + "," + r + " 0 " + large + " 1 " + x2 + "," + y2 + " Z",
        fill: PAL[i % PAL.length] }),
        labels[i] + ": " + fmt(v) + " (" + Math.round((v / total) * 100) + "%)"));
      ang = a2;
    });
    labels.forEach(function (lbl, i) {
      var y = 30 + i * 20;
      svg.appendChild(svgEl("rect", { x: 280, y: y - 10, width: 12, height: 12,
        fill: PAL[i % PAL.length], rx: 2 }));
      svg.appendChild(text(298, y, clip(lbl, 22) + " — " + fmt(values[i]),
        { class: "chart-lbl" }));
    });
    el.appendChild(svg);
  }

  document.querySelectorAll(".js-chart").forEach(function (el) {
    var script = el.querySelector('script[type="application/json"]');
    if (!script) return;
    var data;
    try { data = JSON.parse(script.textContent); } catch (e) { return; }
    var series = (data.series || [])[0];
    if (!series || !series.values || !series.values.length) { noData(el); return; }
    var type = el.dataset.type || "bar";
    if (type === "pie") pie(el, data.labels, series.values);
    else barOrLine(el, type, data.labels, series.values, series.name || "");
  });
})();
