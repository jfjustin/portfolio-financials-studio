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

  function legend(svg, items, x0, y, W) {
    let x = x0;
    items.forEach(it => {
      const w = textW(it.name, 11) + 26;
      if (x + w > W - 4) return;
      svg.appendChild(el("rect", { x, y: y - 8, width: 10, height: 10, rx: 2, fill: it.color,
        "fill-opacity": it.dashed ? 0.35 : 1 }));
      svg.appendChild(el("text", { x: x + 14, y: y + 1, "font-size": 11, fill: "currentColor", "fill-opacity": 0.75 }, it.name));
      x += w;
    });
  }

  // round ticks that always enclose lo..hi (the last tick is at or above the highest value)
  function niceTicks(lo, hi, n) {
    if (hi === lo) { hi = lo + 1; }
    const raw = (hi - lo) / Math.max(1, n), mag = Math.pow(10, Math.floor(Math.log10(Math.abs(raw))));
    const step = [1, 2, 2.5, 5, 10].map(m => m * mag).find(st => st >= raw) || raw;
    const first = Math.floor(lo / step + 1e-9), last = Math.ceil(hi / step - 1e-9);
    const out = [];
    for (let k = first; k <= last; k++) out.push(k * step);
    return out;
  }

  // Vertical columns: categories along the bottom, one bar per series (or one
  // stacked bar), negatives below the zero line; data: {categories, series:[{name, values, color}]}
  function columnChart(container, data, opts) {
    opts = opts || {};
    container.innerHTML = "";
    const cats = (data && data.categories) || [];
    const series = ((data && data.series) || []).filter(s_ => s_.values.some(v => v != null));
    if (!cats.length || !series.length) {
      container.innerHTML = '<div class="muted small">' + (opts.empty || "No data yet.") + "</div>";
      return;
    }
    const f = opts.fmt || fmt, W = container.clientWidth || 520;
    const hasLegend = series.length > 1 || !!opts.legend, hasNotes = !!(opts.notes && opts.notes.some(Boolean));
    const padT = 10 + (hasLegend ? 18 : 0) + (hasNotes ? 16 : 0), padB = 34, padR = 8;
    const H = (opts.height || 240) + (hasLegend ? 18 : 0) + (hasNotes ? 16 : 0) - 16;
    const stacked = !!opts.stacked;
    const tops = cats.map((_, i) => {
      if (!stacked) return series.map(s_ => s_.values[i] || 0);
      let pos = 0, neg = 0;
      series.forEach(s_ => { const v = s_.values[i] || 0; if (v >= 0) pos += v; else neg += v; });
      return [pos, neg];
    }).flat();
    const top = Math.max(0, ...tops), bottom = Math.min(0, ...tops);
    const ticks = niceTicks(bottom - (bottom < 0 ? (top - bottom) * 0.08 : 0), top + (top - bottom) * 0.1, 4);
    const lo = ticks[0], hi = ticks[ticks.length - 1];
    const padL = Math.ceil(Math.max(...ticks.map(t => textW(f(t), 11)))) + 12;
    const plotW = W - padL - padR, plotH = H - padT - padB;
    const y = v => padT + plotH - ((v - lo) / ((hi - lo) || 1)) * plotH;
    const svg = el("svg", { width: "100%", height: H, viewBox: `0 0 ${W} ${H}`, role: "img",
      "aria-label": opts.title || "column chart" });
    if (hasLegend) legend(svg, series, padL, 14, W);
    ticks.forEach(t => {
      svg.appendChild(el("line", { x1: padL, x2: W - padR, y1: y(t), y2: y(t), stroke: "currentColor",
        "stroke-opacity": t === 0 ? 0.45 : 0.1 }));
      svg.appendChild(el("text", { x: padL - 6, y: y(t) + 4, "font-size": 11, "text-anchor": "end",
        fill: "currentColor", "fill-opacity": 0.6 }, f(t)));
    });
    const slot = plotW / cats.length, groupW = Math.min(slot * 0.72, stacked ? 56 : 30 * series.length + 8);
    const barW = stacked ? groupW : groupW / series.length;
    cats.forEach((cat, i) => {
      const x0 = padL + slot * i + (slot - groupW) / 2;
      let pos = 0, neg = 0;
      series.forEach((s_, j) => {
        const v = s_.values[i];
        if (v == null) return;
        let a, b;
        if (stacked) { if (v >= 0) { a = pos; b = pos + v; pos = b; } else { a = neg; b = neg + v; neg = b; } }
        else { a = 0; b = v; }
        const bx = stacked ? x0 : x0 + j * barW, top = y(Math.max(a, b)), h = Math.max(1, Math.abs(y(a) - y(b)));
        const r = el("rect", { class: "bar", x: bx + 1, y: top, width: Math.max(2, barW - 2), height: h, rx: 2,
          fill: s_.color || PALETTE[j % PALETTE.length], "fill-opacity": s_.dashed ? 0.45 : 1 });
        r.appendChild(el("title", {}, `${cat} · ${s_.name}: ${f(v)}`));
        svg.appendChild(r);
        if (!stacked && barW >= textW(f(v), 10) * 0.6 && opts.labels !== false) {
          svg.appendChild(el("text", { x: bx + barW / 2, y: v >= 0 ? top - 3 : top + h + 11, "font-size": 10,
            "text-anchor": "middle", fill: "currentColor", "fill-opacity": 0.7 }, f(v)));
        }
      });
      if (opts.notes && opts.notes[i]) {
        svg.appendChild(el("text", { x: padL + slot * i + slot / 2, y: padT - 6, "font-size": 10.5,
          "text-anchor": "middle", fill: "currentColor", "fill-opacity": 0.6 }, opts.notes[i]));
      }
      svg.appendChild(el("text", { x: padL + slot * i + slot / 2, y: H - padB + 16, "font-size": 11,
        "text-anchor": "middle", fill: "currentColor", "fill-opacity": 0.75 }, fit(cat, slot - 4, 11)));
    });
    container.appendChild(svg);
  }

  // Dated series on a time axis: data {series:[{name, points:[[iso, value]], color, step, dashed, markers}]}
  function timeChart(container, data, opts) {
    opts = opts || {};
    container.innerHTML = "";
    const series = ((data && data.series) || []).filter(s_ => s_.points && s_.points.length);
    if (!series.length) {
      container.innerHTML = '<div class="muted small">' + (opts.empty || "No dated figures yet.") + "</div>";
      return;
    }
    const f = opts.fmt || fmt, W = container.clientWidth || 640, H = opts.height || 240;
    const t = iso => new Date(iso + "T00:00:00Z").getTime();
    const all = series.flatMap(s_ => s_.points);
    let t0 = Math.min(...all.map(p => t(p[0]))), t1 = Math.max(...all.map(p => t(p[0])));
    if (t1 === t0) { t0 -= 86400000 * 90; t1 += 86400000 * 90; }
    const vmax = Math.max(0, ...all.map(p => p[1])), vmin = Math.min(0, ...all.map(p => p[1]));
    const ticks = niceTicks(vmin, vmax + (vmax - vmin) * 0.06, 4);
    const lo = ticks[0], hi = ticks[ticks.length - 1];
    const padL = Math.ceil(Math.max(...ticks.map(v => textW(f(v), 11)))) + 12, padR = 14, padT = 30, padB = 26;
    const plotW = W - padL - padR, plotH = H - padT - padB;
    const x = iso => padL + ((t(iso) - t0) / (t1 - t0)) * plotW;
    const y = v => padT + plotH - ((v - lo) / ((hi - lo) || 1)) * plotH;
    const svg = el("svg", { width: "100%", height: H, viewBox: `0 0 ${W} ${H}`, role: "img",
      "aria-label": opts.title || "time series" });
    legend(svg, series, padL, 14, W);
    ticks.forEach(v => {
      svg.appendChild(el("line", { x1: padL, x2: W - padR, y1: y(v), y2: y(v), stroke: "currentColor",
        "stroke-opacity": v === 0 ? 0.4 : 0.1 }));
      svg.appendChild(el("text", { x: padL - 6, y: y(v) + 4, "font-size": 11, "text-anchor": "end",
        fill: "currentColor", "fill-opacity": 0.6 }, f(v)));
    });
    // years along the bottom, thinned to fit
    const y0 = new Date(t0).getUTCFullYear(), y1 = new Date(t1).getUTCFullYear();
    const every = Math.max(1, Math.ceil((y1 - y0 + 1) * 40 / plotW));
    for (let yr = y0; yr <= y1 + 1; yr++) {
      const iso = `${yr}-01-01`;
      if (t(iso) < t0 || t(iso) > t1 || (yr - y0) % every) continue;
      svg.appendChild(el("line", { x1: x(iso), x2: x(iso), y1: padT, y2: padT + plotH, stroke: "currentColor", "stroke-opacity": 0.06 }));
      svg.appendChild(el("text", { x: x(iso), y: H - 8, "font-size": 11, "text-anchor": "middle",
        fill: "currentColor", "fill-opacity": 0.6 }, String(yr)));
    }
    series.forEach((s_, j) => {
      const color = s_.color || PALETTE[j % PALETTE.length];
      const pts = s_.points.slice().sort((a, b) => t(a[0]) - t(b[0]));
      let d = "";
      pts.forEach((p, i) => {
        if (i === 0) d += `M${x(p[0])},${y(p[1])}`;
        else if (s_.step) d += `H${x(p[0])}V${y(p[1])}`;
        else d += `L${x(p[0])},${y(p[1])}`;
      });
      if (s_.step && pts.length) d += `H${padL + plotW}`;
      if (pts.length > 1 || s_.step)
        svg.appendChild(el("path", { d, fill: "none", stroke: color, "stroke-width": 2,
          "stroke-dasharray": s_.dashed ? "5 4" : null }));
      if (s_.markers !== false) pts.forEach(p => {
        const c = el("circle", { cx: x(p[0]), cy: y(p[1]), r: pts.length > 40 ? 2 : 3.5, fill: color });
        c.appendChild(el("title", {}, `${s_.name} · ${p[0]}: ${f(p[1])}${p[2] ? " · " + p[2] : ""}`));
        svg.appendChild(c);
      });
    });
    container.appendChild(svg);
  }

  // One bar split by share: items [{label, value, color}]
  function allocationBar(container, items, opts) {
    opts = opts || {};
    container.innerHTML = "";
    const total = items.reduce((a, it) => a + Math.max(0, it.value || 0), 0);
    if (!total) { container.innerHTML = '<div class="muted small">No data yet.</div>'; return; }
    const W = container.clientWidth || 400, H = 54;
    const svg = el("svg", { width: "100%", height: H, viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": opts.title || "allocation" });
    let x = 0;
    items.forEach((it, i) => {
      const w = (Math.max(0, it.value || 0) / total) * W, color = it.color || PALETTE[i % PALETTE.length];
      const r = el("rect", { x, y: 4, width: Math.max(0, w - 2), height: 18, rx: 3, fill: color });
      r.appendChild(el("title", {}, `${it.label}: ${(it.value / total * 100).toFixed(1)}%`));
      svg.appendChild(r);
      const label = `${it.label} ${(it.value / total * 100).toFixed(0)}%`;
      if (textW(label, 11) < w - 4)
        svg.appendChild(el("text", { x: x + 4, y: 42, "font-size": 11, fill: "currentColor", "fill-opacity": 0.75 }, label));
      x += w;
    });
    container.appendChild(svg);
  }

  window.Charts = { barChart, lineChart, columnChart, timeChart, allocationBar, fmt };
})();
