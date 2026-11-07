// The StoryCraft mark, animated as in the app's loading screen (src/gui/brand.py, draw_mark_animated):
// the S is traced as a story arc (1.4 s), held (0.8 s) and faded (0.4 s) while the playhead glides
// over the clips and lights the one it crosses. Runs only while the mark is on screen.
(function () {
  "use strict";
  const CYCLE = 2.6, TRACE = 1.4, FADE_AT = 2.2;
  const S = "M 344 148 C 322 116 288 102 252 102 C 198 102 164 134 164 178 C 164 226 204 242 256 256 " +
            "C 308 270 348 288 348 336 C 348 382 312 410 260 410 C 218 410 186 394 166 362";
  const BARS = [[190, 210, 150, false], [128, 246, 196, true], [204, 282, 150, false]]; // x, y, width, hot
  const NS = "http://www.w3.org/2000/svg";
  const reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  let uid = 0;

  const el = (tag, attrs, parent) => {
    const n = document.createElementNS(NS, tag);
    for (const k in attrs) n.setAttribute(k, attrs[k]);
    if (parent) parent.appendChild(n);
    return n;
  };
  const grad = (defs, id, x1, y1, x2, y2, a, b, op) => {
    const g = el("linearGradient", { id, x1, y1, x2, y2, gradientUnits: "userSpaceOnUse" }, defs);
    el("stop", { offset: 0, "stop-color": a, "stop-opacity": op }, g);
    el("stop", { offset: 1, "stop-color": b, "stop-opacity": op }, g);
  };

  function build(svg) {
    const p = "m" + (++uid) + "-";
    svg.setAttribute("viewBox", "0 0 512 512");
    svg.textContent = "";
    const defs = el("defs", {}, svg);
    grad(defs, p + "arc", 164, 102, 348, 410, "#22d3ee", "#a855f7", 1);
    grad(defs, p + "hot", 112, 0, 348, 0, "#22d3ee", "#a855f7", 1);
    grad(defs, p + "tile", 0, 0, 512, 512, "#1a1b2e", "#0b0b14", 1);
    grad(defs, p + "rim", 0, 0, 512, 512, "#22d3ee", "#a855f7", 0.55);
    el("rect", { x: 8, y: 8, width: 496, height: 496, rx: 116, fill: `url(#${p}tile)` }, svg);
    el("rect", { x: 9.5, y: 9.5, width: 493, height: 493, rx: 114.5, fill: "none", stroke: `url(#${p}rim)`, "stroke-width": 3 }, svg);
    const line = { fill: "none", "stroke-width": 54, "stroke-linecap": "round", "stroke-linejoin": "round", d: S };
    el("path", Object.assign({ stroke: "rgba(255,255,255,0.133)" }, line), svg);
    const arc = el("path", Object.assign({ stroke: `url(#${p}arc)` }, line), svg);
    const bars = BARS.map(([x, y, w, hot]) => {
      el("rect", { x: x - 5, y: y - 5, width: w + 10, height: 27, rx: 13.5, fill: "#11121f" }, svg);
      const bar = el("rect", { x, y, width: w, height: 17, rx: 8.5, fill: hot ? `url(#${p}hot)` : "#c9cbe0" }, svg);
      const glow = hot ? el("rect", { x, y, width: w, height: 17, rx: 8.5, fill: "rgba(255,255,255,0.235)", opacity: 0 }, svg) : null;
      return { x, w, hot, bar, glow };
    });
    const head = el("g", {}, svg);
    el("rect", { x: -5, y: 176, width: 10, height: 150, rx: 5, fill: "#11121f" }, head);
    el("rect", { x: -3, y: 180, width: 6, height: 142, rx: 3, fill: "#ffffff" }, head);
    el("circle", { cx: 0, cy: 174, r: 11, fill: "#ffffff", stroke: "#11121f", "stroke-width": 5 }, head);
    const len = arc.getTotalLength ? arc.getTotalLength() : 1000;
    arc.setAttribute("stroke-dasharray", `${len} ${len}`);
    return { arc, bars, head, len };
  }

  const ease = (x) => 0.5 - 0.5 * Math.cos(Math.PI * Math.min(1, Math.max(0, x)));

  function draw(m, t) {
    const phase = t % CYCLE;
    const progress = ease(phase / TRACE);
    const bright = phase < FADE_AT ? 1 : 1 - (phase - FADE_AT) / (CYCLE - FADE_AT);
    const headX = 150 + 196 * (0.5 - 0.5 * Math.cos(2 * Math.PI * t / CYCLE));
    m.arc.setAttribute("stroke-dashoffset", m.len * (1 - progress));
    m.arc.setAttribute("opacity", progress > 0.002 ? bright : 0);
    m.head.setAttribute("transform", `translate(${headX} 0)`);
    for (const b of m.bars) {
      const under = b.x <= headX && headX <= b.x + b.w;
      if (b.hot) b.glow.setAttribute("opacity", under ? 1 : 0);
      else b.bar.setAttribute("fill", under ? "#ffffff" : "#c9cbe0");
    }
  }

  document.querySelectorAll("svg[data-mark]").forEach((svg) => {
    const m = build(svg);
    if (reduced) { draw(m, 1.8); return; } // the S traced, still
    let t0 = null, raf = null;
    const frame = (now) => {
      if (t0 === null) t0 = now;
      draw(m, (now - t0) / 1000);
      raf = requestAnimationFrame(frame);
    };
    draw(m, 0);
    const start = () => { if (raf === null) raf = requestAnimationFrame(frame); };
    const stop = () => { if (raf !== null) cancelAnimationFrame(raf); raf = null; };
    if ("IntersectionObserver" in window) {
      new IntersectionObserver((es) => es.forEach((e) => (e.isIntersecting ? start() : stop()))).observe(svg);
    } else {
      start();
    }
  });
})();
