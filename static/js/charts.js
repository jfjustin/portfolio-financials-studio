/* Tiny dependency-free SVG charts. No CDN, no external libs — fully offline. */
(function () {
  const NS = "http://www.w3.org/2000/svg";
  // A single-hue-family categorical palette that reads in light & dark.
  const PALETTE = ["#1f5f8b", "#2e8b6f", "#b7791f", "#8b5cf6", "#c0392b",
                   "#0f766e", "#a16207", "#4b6bfb", "#be185d", "#15803d"];

  function el(tag, attrs, text) {
    const n = document.createElementNS(NS, tag);
    for (const k in (attrs || {})) n.setAttribute(k, attrs[k]);
    if (text != null) n.textContent = text;
    return n;
  }

  function fmt(v) {
    const n = Number(v) || 0;
    const abs = Math.abs(n);
    if (abs >= 1e9) return (n / 1e9).toFixed(2) + "B";
    if (abs >= 1e6) return (n / 1e6).toFixed(2) + "M";
    if (abs >= 1e3) return (n / 1e3).toFixed(1) + "K";
    return n.toLocaleString();
  }

  // data: [{label, value}]
  function barChart(container, data, opts) {
    opts = opts || {};
    container.innerHTML = "";
    if (!data || !data.length) {
      container.innerHTML = '<div class="muted small">No trusted data yet.</div>';
      return;
    }
    const rowH = 30, padL = 8, padR = 64, padT = 6, gap = 8;
    const W = container.clientWidth || 520;
    const labelW = Math.min(180, Math.max(90, W * 0.32));
    const chartW = W - labelW - padL - padR;
    const H = padT * 2 + data.length * rowH;
    const max = Math.max.apply(null, data.map(d => Math.abs(Number(d.value) || 0))) || 1;

    const svg = el("svg", { width: "100%", height: H, viewBox: `0 0 ${W} ${H}`,
      role: "img", "aria-label": opts.title || "bar chart" });

    data.forEach((d, i) => {
      const y = padT + i * rowH;
      const val = Number(d.value) || 0;
      const w = Math.max(2, (Math.abs(val) / max) * chartW);
      const color = PALETTE[i % PALETTE.length];
      // label (truncated)
      const label = String(d.label);
      const short = label.length > 22 ? label.slice(0, 21) + "…" : label;
      const t = el("text", { x: padL, y: y + rowH / 2 + 4, "font-size": 12,
        fill: "currentColor" }, short);
      t.appendChild(el("title", {}, label));
      svg.appendChild(t);
      // bar
      const rect = el("rect", { class: "bar", x: labelW + padL, y: y + 4,
        width: w, height: rowH - gap, rx: 4, fill: color });
      rect.appendChild(el("title", {}, `${label}: ${fmt(val)}`));
      svg.appendChild(rect);
      // value
      svg.appendChild(el("text", { x: labelW + padL + w + 6, y: y + rowH / 2 + 4,
        "font-size": 12, fill: "currentColor" }, fmt(val)));
    });
    container.appendChild(svg);
  }

  window.Charts = { barChart, fmt };
})();
