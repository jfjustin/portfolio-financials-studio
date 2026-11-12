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

  // Text width in the page's own font, so a chart in a narrow section shortens its
  // labels instead of drawing them over the bars or past its edge.
  const CTX = document.createElement("canvas").getContext("2d");
  function textW(s, size, weight) {
    CTX.font = `${weight || 400} ${size}px ${getComputedStyle(document.body).fontFamily}`;
    return CTX.measureText(String(s)).width;
  }
  function fit(s, maxW, size) {
    s = String(s);
    if (textW(s, size) <= maxW) return s;
    while (s.length > 1 && textW(s + "…", size) > maxW) s = s.slice(0, -1);
    return s + "…";
  }
  const widest = (items, size, weight) => Math.max.apply(null, items.map(s => textW(s, size, weight)).concat([0]));

  // data: [{label, value}]
  function barChart(container, data, opts) {
    opts = opts || {};
    container.innerHTML = "";
    if (!data || !data.length) {
      container.innerHTML = '<div class="muted small">No trusted data yet.</div>';
      return;
    }
    const rowH = 30, padL = 8, padT = 6, gap = 8, fs = 12;
    const W = container.clientWidth || 520;
    const padR = widest(data.map(d => fmt(d.value)), fs) + 12;          // the value after the longest bar
    const labelW = Math.min(widest(data.map(d => d.label), fs) + 12, Math.max(64, W * 0.38), 200);
    const chartW = Math.max(16, W - labelW - padL - padR);
    const H = padT * 2 + data.length * rowH;
    const max = Math.max.apply(null, data.map(d => Math.abs(Number(d.value) || 0))) || 1;

    const svg = el("svg", { width: "100%", height: H, viewBox: `0 0 ${W} ${H}`,
      role: "img", "aria-label": opts.title || "bar chart" });

    data.forEach((d, i) => {
      const y = padT + i * rowH;
      const val = Number(d.value) || 0;
      const w = Math.max(2, (Math.abs(val) / max) * chartW);
      const color = PALETTE[i % PALETTE.length];
      // label (shortened to its column; the full name is in the tooltip)
      const label = String(d.label);
      const t = el("text", { x: padL, y: y + rowH / 2 + 4, "font-size": fs,
        fill: "currentColor" }, fit(label, labelW - 8, fs));
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
    const fiscalLabels = data.fiscal_labels || {};
    const W = container.clientWidth || 640;
    // the latest period's labels and the end value are centred on the last point: keep them inside
    const last = periods[periods.length - 1];
    const lastTotal = (data.total || []).filter(p => p.period === last)[0];
    const padR = Math.ceil(Math.max(16, textW(last, 11) / 2 + 4, textW(fiscalLabels[last] || "", 10) / 2 + 4,
                                    lastTotal ? textW(fmt(lastTotal.value), 12, 700) / 2 + 4 : 0));
    const H = 240, padL = 54, padT = 14, padB = 46;  // padB: 2 lines (calendar + FY)
    const plotW = W - padL - padR, plotH = H - padT - padB;
    // label every period while they fit, else every 2nd / 3rd …, always including the latest
    const labelW = Math.max(widest(periods, 11), widest(periods.map(p => fiscalLabels[p] || ""), 10)) + 10;
    const every = periods.length > 1 ? Math.max(1, Math.ceil(labelW / (plotW / (periods.length - 1)))) : 1;
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
    // x labels — calendar period on top, fiscal-year label underneath
    periods.forEach((p, i) => {
      if ((periods.length - 1 - i) % every) return;
      svg.appendChild(el("text", { x: x(i), y: H - 24, "font-size": 11,
        "text-anchor": "middle", fill: "currentColor", "fill-opacity": 0.7 }, p));
      const fy = fiscalLabels[p];
      if (fy)
        svg.appendChild(el("text", { x: x(i), y: H - 10, "font-size": 10,
          "text-anchor": "middle", fill: "currentColor", "fill-opacity": 0.45 }, fy));
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
