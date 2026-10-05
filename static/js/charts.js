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

  // data: { periods:[str], total:[{period,value}], series:[{name, points:[{period,value}]}] }
  function lineChart(container, data, opts) {
    opts = opts || {};
    container.innerHTML = "";
    const periods = (data && data.periods) || [];
    if (periods.length === 0) {
      container.innerHTML = '<div class="muted small">No time-series data yet — confirm values across at least one period.</div>';
      return;
    }
    const W = container.clientWidth || 640;
    const H = 240, padL = 54, padR = 16, padT = 14, padB = 34;
    const plotW = W - padL - padR, plotH = H - padT - padB;
    const xi = {}; periods.forEach((p, i) => xi[p] = i);
    const x = i => padL + (periods.length === 1 ? plotW / 2 : (i / (periods.length - 1)) * plotW);

    const allVals = [].concat(
      (data.total || []).map(d => d.value),
      ...(data.series || []).map(s => s.points.map(p => p.value)));
    const maxV = Math.max.apply(null, allVals.concat([1]));
    const minV = Math.min.apply(null, allVals.concat([0]));
    const y = v => padT + plotH - ((v - minV) / ((maxV - minV) || 1)) * plotH;

    const svg = el("svg", { width: "100%", height: H, viewBox: `0 0 ${W} ${H}`,
      role: "img", "aria-label": opts.title || "time series" });

    // gridlines + y labels (0, mid, max)
    [minV, (minV + maxV) / 2, maxV].forEach(gv => {
      const yy = y(gv);
      svg.appendChild(el("line", { x1: padL, y1: yy, x2: W - padR, y2: yy,
        stroke: "currentColor", "stroke-opacity": 0.12 }));
      svg.appendChild(el("text", { x: padL - 8, y: yy + 4, "font-size": 11,
        "text-anchor": "end", fill: "currentColor", "fill-opacity": 0.6 }, fmt(gv)));
    });
    // x labels
    periods.forEach((p, i) => {
      svg.appendChild(el("text", { x: x(i), y: H - 10, "font-size": 11,
        "text-anchor": "middle", fill: "currentColor", "fill-opacity": 0.7 }, p));
    });

    const pointsStr = pts => pts.map(p => `${x(xi[p.period])},${y(p.value)}`).join(" ");

    // faint entity lines
    (data.series || []).forEach((s, idx) => {
      const pts = s.points.filter(p => p.period in xi);
      if (pts.length < 1) return;
      const color = PALETTE[(idx + 1) % PALETTE.length];
      if (pts.length > 1)
        svg.appendChild(el("polyline", { points: pointsStr(pts), fill: "none",
          stroke: color, "stroke-width": 1.5, "stroke-opacity": 0.55 }));
      pts.forEach(p => {
        const c = el("circle", { cx: x(xi[p.period]), cy: y(p.value), r: 2.5,
          fill: color, "fill-opacity": 0.7 });
        c.appendChild(el("title", {}, `${s.name} · ${p.period}: ${fmt(p.value)}`));
        svg.appendChild(c);
      });
    });

    // emphasized total: area + line + endpoint
    const total = (data.total || []).filter(p => p.period in xi);
    if (total.length) {
      const brand = PALETTE[0];
      if (total.length > 1) {
        const area = `${padL + 0},${y(minV)} ` +
          total.map(p => `${x(xi[p.period])},${y(p.value)}`).join(" ") +
          ` ${x(xi[total[total.length - 1].period])},${y(minV)}`;
        svg.appendChild(el("polygon", { points: area, fill: brand, "fill-opacity": 0.10 }));
        svg.appendChild(el("polyline", { points: pointsStr(total), fill: "none",
          stroke: brand, "stroke-width": 2.5 }));
      }
      total.forEach((p, i) => {
        const last = i === total.length - 1;
        const c = el("circle", { cx: x(xi[p.period]), cy: y(p.value),
          r: last ? 4.5 : 3, fill: brand });
        c.appendChild(el("title", {}, `Total · ${p.period}: ${fmt(p.value)}`));
        svg.appendChild(c);
        if (last)
          svg.appendChild(el("text", { x: x(xi[p.period]), y: y(p.value) - 9,
            "font-size": 12, "font-weight": 700, "text-anchor": "middle",
            fill: "currentColor" }, fmt(p.value)));
      });
    }
    container.appendChild(svg);
  }

  window.Charts = { barChart, lineChart, fmt };
})();
