/* Portfolio Financials Studio — dashboard SPA logic. All calls are same-origin. */
(function () {
  const $ = (s, r) => (r || document).querySelector(s);
  const $$ = (s, r) => Array.from((r || document).querySelectorAll(s));
  const api = (p, o) => fetch(p, o).then(r => r.json());
  let REPORT_TYPES = {};

  function toast(msg) {
    const t = $("#toast"); t.textContent = msg; t.classList.add("show");
    setTimeout(() => t.classList.remove("show"), 2200);
  }
  const money = (v, cur) => (cur ? cur + " " : "") + (window.Charts.fmt(v));

  // ---------------- boot ----------------
  async function boot() {
    REPORT_TYPES = await api("/api/report-types");
    const h = await api("/api/health");
    const b = $("#llm-badge");
    const ps = h.provider_status || {};
    if (!h.llm_enabled) { b.textContent = "Deterministic only (no model)"; b.className = "badge"; }
    else if (ps.available) {
      b.textContent = h.llm_provider + (ps.deployment || ps.model ? " · " + (ps.deployment || ps.model) : "") + " ✓";
      b.className = "badge on"; }
    else { b.textContent = h.llm_provider + " unavailable"; b.className = "badge off"; }
    if (ps.detail) b.title = ps.detail;
    $("#ledger-filter").addEventListener("input", renderLedger);
    $("#dif-year").addEventListener("change", e => loadDirectInvestments(+e.target.value));
    $("#dif-period").addEventListener("change", renderDif);
    $("#pf-asof").addEventListener("change", e => loadPortfolio(e.target.value));
    wirePortfolioModal();
    wireManualForms();
    await refresh();
    loadTerms();
    loadLake();
  }

  // "Clear data": empty the database after an explicit typed confirmation
  async function clearData() {
    const typed = window.prompt(
      "This deletes ALL uploaded submissions and extracted values for everyone " +
      "using this site. The audit trail is kept and a backup snapshot is saved.\n\n" +
      "Type CLEAR to confirm:");
    if (typed === null) return;
    if (typed.trim() !== "CLEAR") { toast("Not cleared — you must type CLEAR."); return; }
    const res = await fetch("/api/admin/clear", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ confirm: "CLEAR" }) });
    if (!res.ok) { toast("Clear failed: " + (await res.text())); return; }
    const r = await res.json();
    toast(`Cleared ${r.cleared_submissions} submissions, ${r.cleared_facts} values` +
          (r.backup ? ` · backup ${r.backup}` : ""));
    await refresh();
  }

  async function refresh() {
    const d = await api("/api/dashboard");
    $("#t-subs").textContent = d.n_submissions;
    $("#t-facts").textContent = d.n_facts;
    $("#t-entities").textContent = d.n_entities;
    const rev = $("#t-review"); rev.textContent = d.n_pending_review;
    rev.classList.toggle("warn", d.n_pending_review > 0);

    const byType = Object.entries(d.value_by_type).map(([k, v]) =>
      ({ label: (REPORT_TYPES[k] && REPORT_TYPES[k].label) || k, value: v }));
    const byEntity = Object.entries(d.value_by_entity).map(([k, v]) => ({ label: k, value: v }));
    window.Charts.barChart($("#chart-type"), byType, { title: "Value by report type" });
    window.Charts.barChart($("#chart-entity"), byEntity, { title: "Value by entity" });

    const s = await api("/api/submissions");
    renderSubs(s.submissions);

    const inv = await api("/api/investments");
    renderInvestments(inv);

    const perf = await api("/api/performance");
    renderPerformance(perf);
    await loadDirectInvestments(DIF ? DIF.year : null);
    await loadPortfolio(PF ? PF.as_of : null);

    const ts = await api("/api/timeseries");
    window.Charts.lineChart($("#chart-time"), ts, { title: "Portfolio value over time" });

    const lg = await api("/api/ledger");
    LEDGER = lg.rows;
    renderLedger();
  }

  let LEDGER = [];
  function renderLedger() {
    const q = ($("#ledger-filter").value || "").toLowerCase();
    const rows = LEDGER.filter(r => !q ||
      (r.entity_name + " " + r.metric_label + " " + r.period + " " +
       r.report_type + " " + (r.category_label || r.category))
        .toLowerCase().includes(q));
    $("#ledger-count").textContent = `${rows.length} of ${LEDGER.length} entries`;
    const tb = $("#ledger-body"); tb.innerHTML = "";
    if (!rows.length) {
      tb.innerHTML = '<tr><td colspan="7" class="muted">No trusted ledger entries yet — upload and confirm reports.</td></tr>';
      return;
    }
    rows.slice(0, 600).forEach(r => {
      const tr = document.createElement("tr");
      const val = r.unit === "currency" ? mv(r.value)
        : (r.value == null ? '<span class="muted">—</span>'
           : esc(r.value) + (r.unit === "percent" ? "%" : ""));
      tr.innerHTML = `<td>${esc(r.period)}${r.fiscal_label ? `<div class="small muted fy">${esc(r.fiscal_label)}</div>` : ""}</td>
        <td><b>${esc(r.entity_name)}</b></td>
        <td><span class="tag cat-${esc(r.category)}">${esc(r.category_label || r.category)}</span></td>
        <td>${esc(r.metric_label)}</td>
        <td class="num">${val}</td><td>${esc(r.currency || "")}</td>
        <td class="small muted">${esc(r.source || "")}</td>`;
      tb.appendChild(tr);
    });
  }

  // ---------------- Direct Investment Financials (Lam's quarterly report) ----------------
  let DIF = null;
  const DIF_BASE = ["revenue", "operating_cash_flow", "capex", "ending_cash", "operating_expense"];
  const DIF_COLS = DIF_BASE.concat(["gross_monthly_burn", "gross_runway", "net_monthly_burn", "net_runway"]);
  // $000 with negatives in parentheses, as Lam's report prints them
  const kfmt = v => {
    if (v == null || v === "") return '<span class="muted">—</span>';
    const k = Math.round(Number(v) / 1000);
    const s = "$" + Math.abs(k).toLocaleString("en-US");
    return k < 0 ? `(${s})` : s;
  };
  const runwayFmt = (v, burn) => v != null ? `${v} Months`
    : (burn != null && Number(burn) <= 0
        ? '<span class="muted" title="Not burning cash in this period">n/a</span>'
        : '<span class="muted">—</span>');

  async function loadDirectInvestments(year) {
    DIF = await api("/api/direct-investments" + (year ? "?year=" + year : ""));
    $("#dif-year").innerHTML = DIF.years.map(y =>
      `<option value="${y}" ${y === DIF.year ? "selected" : ""}>${y}</option>`).join("");
    const ps = $("#dif-period"), keep = ps.value, yy = String(DIF.year).slice(2);
    ps.innerHTML = DIF.periods.map((p, i) =>
      `<option value="${p}">${i < 4 ? `Q${i + 1}'${yy}` : `${DIF.year} (year)`}</option>`).join("");
    ps.value = DIF.periods.includes(keep) ? keep : (DIF.latest_quarter || DIF.periods[0]);
    $("#dif-companies").innerHTML = DIF.companies.map(c => `<option value="${esc(c.company)}">`).join("");
    $("#dif-manual-q").innerHTML = DIF.periods.slice(0, 4).map(p =>
      `<option ${p === (DIF.latest_quarter || DIF.periods[0]) ? "selected" : ""}>${p}</option>`).join("");
    renderDif();
  }

  function renderDif() {
    const per = $("#dif-period").value;
    const q = per.includes("-Q") ? per.slice(-1) : "";
    $("#dif-export").href = `/api/exports/direct-investment-financials.xlsx?year=${DIF.year}` +
      (q ? `&quarter=${q}` : "");
    const tb = $("#dif-body"); tb.innerHTML = "";
    if (!DIF.companies.length) {
      tb.innerHTML = '<tr><td colspan="11" class="muted" style="text-align:left">No direct-investment ' +
        "figures yet — upload a company's P&amp;L, balance sheet and cash-flow statement, or enter " +
        "them by hand below.</td></tr>";
      $("#dif-attention").innerHTML = "";
      return;
    }
    const editable = per.includes("-Q") || /^\d{4}$/.test(per);
    const quarter = per.includes("-Q");
    DIF.companies.forEach(c => {
      const col = c.columns[per] || { actual: {}, budget: {}, sources: {}, pending: [], checks: [] };
      const tr = document.createElement("tr");
      tr.innerHTML = `<td class="lbl" rowspan="2">${esc(c.company)}` +
        (c.currency !== "USD" ? ` <span class="small muted">${esc(c.currency)}</span>` : "") +
        ((col.checks || []).length ? `<div class="small warn-text" title="${esc(col.checks.join("\n"))}">` +
          "⚠ differs from the file's own burn/runway</div>" : "") +
        `</td><td class="lbl small">Actual</td>` +
        DIF_COLS.map(k => {
          const v = col.actual[k], src = (col.sources || {})[k], pend = (col.pending || {})[k];
          const clash = (col.issues || []).find(i => i.kind === "mismatch" && i.metric === k);
          const attrs = `data-entity="${esc(c.company)}" data-period="${esc(per)}" data-metric="${k}"`;
          let cell;
          if (DIF_BASE.includes(k) && quarter && v == null && pend == null) {
            // in no approved file: a person checks the source and types it in
            cell = `<input class="dif-in dif-act missing" ${attrs} placeholder="enter" inputmode="decimal" ` +
              `title="Not in any approved file — check the source, then type the figure ($000)" ` +
              `aria-label="${esc(c.company)} ${esc(k)} actual, $000">`;
          } else if (DIF_BASE.includes(k) && quarter && src && src.manual) {
            cell = `<input class="dif-in dif-act manual" ${attrs} value="${Math.round(Number(v) / 1000)}" ` +
              `inputmode="decimal" title="Entered by hand — change it, or clear it to withdraw it" ` +
              `aria-label="${esc(c.company)} ${esc(k)} actual, $000, entered by hand">`;
          } else if (v == null && pend != null) {
            cell = `<button class="tag cat-ratio dif-open" data-sub="${pend}" ` +
              `title="Read from a file — waiting for review. Click to open it">review</button>`;
          } else {
            cell = k.endsWith("runway") ? runwayFmt(v, col.actual[k.replace("runway", "monthly_burn")]) : kfmt(v);
            if (src) cell = `<a href="#" class="dif-open" data-sub="${src.submission_id}">${cell}</a>`;
          }
          if (clash) cell += ` <span class="warn-text" title="${esc(clash.message)}">⚠</span>`;
          const title = src ? `${src.file} · "${src.line}" (${src.status}) — click to open` : "";
          const neg = DIF_BASE.includes(k) && v != null && Number(v) < 0;
          return `<td class="num ${neg ? "is-neg" : ""}" title="${esc(title)}">${cell}</td>`;
        }).join("");
      tb.appendChild(tr);
      const br = document.createElement("tr");
      br.className = "dif-budget";
      br.innerHTML = '<td class="lbl small muted">Budget</td>' + DIF_COLS.map(k => {
        const v = col.budget[k];
        if (DIF_BASE.includes(k) && editable) {
          const val = v == null ? "" : Math.round(Number(v) / 1000);
          return `<td class="num"><input class="dif-in" data-entity="${esc(c.company)}" ` +
            `data-period="${esc(per)}" data-metric="${k}" value="${val}" placeholder="—" ` +
            `inputmode="decimal" aria-label="${esc(c.company)} ${esc(k)} budget, $000"></td>`;
        }
        return `<td class="num muted">${k.endsWith("runway")
          ? runwayFmt(v, col.budget[k.replace("runway", "monthly_burn")]) : kfmt(v)}</td>`;
      }).join("");
      tb.appendChild(br);
    });
    $$(".dif-in:not(.dif-act)", tb).forEach(inp => inp.addEventListener("change", saveBudget));
    $$(".dif-act", tb).forEach(inp => inp.addEventListener("change", saveActual));
    $$(".dif-open", tb).forEach(el => el.addEventListener("click", e => {
      e.preventDefault(); openSub(+el.dataset.sub); }));
    renderAttention("#dif-attention", DIF.companies.flatMap(c =>
      ((c.columns[per] || {}).issues || []).map(i => Object.assign({ who: c.company }, i))), null);
  }

  // a figure typed in by hand (an empty, unreadable or incomplete statement)
  async function saveManual(body) {
    const res = await fetch("/api/manual-values", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(Object.assign({ report_type: "company_financials", scale: 1000 }, body)) });
    if (!res.ok) { toast("Not saved: " + (await res.json()).detail); return false; }
    toast(String(body.value || "").trim() ? "Figure saved — entered by hand, audited" : "Entered figure withdrawn");
    await loadDirectInvestments(DIF.year);
    return true;
  }
  const saveActual = e => saveManual({ entity: e.target.dataset.entity, period: e.target.dataset.period,
                                       metric: e.target.dataset.metric, value: e.target.value });

  // "Needs attention": what a person has to check, enter or settle
  const KIND = { missing: "missing", review: "to review", mismatch: "mismatch", check: "check",
                 estimate: "estimate", unread: "unread" };
  function renderAttention(sel, items, onEnter) {
    const box = $(sel);
    if (!items.length) { box.innerHTML = ""; return; }
    box.innerHTML = `<div class="att-head">Needs attention · ${items.length}</div>` + items.map((i, n) =>
      `<div class="att-item ${esc(i.kind)}"><span class="att-kind">${esc(KIND[i.kind] || i.kind)}</span>` +
      `<span><b>${esc(i.who)}</b> — ${esc(i.message)}</span>` +
      (i.submission_id ? ` <button class="btn sm att-open" data-sub="${i.submission_id}">Open file</button>` : "") +
      (onEnter && i.kind !== "review" ? ` <button class="btn sm att-enter" data-n="${n}">Enter</button>` : "") +
      `</div>`).join("");
    $$(".att-open", box).forEach(b => b.onclick = () => openSub(+b.dataset.sub));
    $$(".att-enter", box).forEach(b => b.onclick = () => onEnter(items[+b.dataset.n]));
  }

  async function saveBudget(e) {
    const i = e.target;
    const res = await fetch("/api/budgets", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ entity: i.dataset.entity, period: i.dataset.period,
                             metric: i.dataset.metric, value: i.value, scale: 1000 }) });
    if (!res.ok) { toast("Budget not saved: " + (await res.text())); return; }
    toast(i.value.trim() ? "Budget saved" : "Budget cleared");
    await loadDirectInvestments(DIF.year);
  }

  // ---------------- Investment Portfolio (Lam's quarterly report) ----------------
  let PF = null, PF_ENTITY = null;
  // $M as Lam's report prints them
  const mfmt = (v, d) => {
    if (v == null || v === "") return '<span class="muted">—</span>';
    const n = Number(v) / 1e6, s = "$" + Math.abs(n).toFixed(d == null ? 1 : d);
    return n < 0 ? `(${s})` : s;
  };
  const srcTitle = (r, k) => esc((r.sources || {})[k] || "");
  // a figure built only from the quarters on file, until a statement gives the since-inception one
  const estMark = (r, k) => ((r.sources || {})[k] === "entered" || (r.sources || {})[k] === "fund parameters"
      ? '<sup class="man" title="Entered by hand">✎</sup>' : "")
    + ((r.estimated || []).includes(k)
      ? '<sup class="est" title="Estimate — only the quarters on file">*</sup>' : "");

  async function loadPortfolio(asOf) {
    PF = await api("/api/portfolio" + (asOf ? "?as_of=" + asOf : ""));
    $("#pf-asof").innerHTML = PF.quarters.slice().reverse().map(q =>
      `<option value="${q}" ${q === PF.as_of ? "selected" : ""}>${q}</option>`).join("");
    $("#pf-export").href = "/api/exports/investment-portfolio.xlsx?as_of=" + PF.as_of;
    const fb = $("#pf-funds"); fb.innerHTML = "";
    if (!PF.funds.length)
      fb.innerHTML = '<tr><td colspan="12" class="muted" style="text-align:left">No fund data yet — upload capital-account statements or JV workbooks.</td></tr>';
    PF.funds.forEach(f => {
      const st = f.stats || {};
      const parts = [st.as_of ? esc(st.as_of) : "", st.count != null ? st.count + " inv." : "",
        st.cost != null || st.value != null ? `${mfmt(st.cost)} → ${mfmt(st.value)}` : "",
        st.gain_pct != null ? `<span class="${st.gain_pct < 0 ? "is-neg" : "is-pos"}">${st.gain_pct >= 0 ? "+" : ""}${st.gain_pct.toFixed(1)}%</span>` : ""
      ].filter(Boolean);
      const stats = parts.length ? parts.join(" · ") : '<span class="muted">—</span>';
      const tr = document.createElement("tr");
      tr.innerHTML = `<td class="lbl"><a href="#" class="pf-open" data-entity="${esc(f.entity)}">${esc(f.investment_name)}</a>` +
          (f.investment_name !== f.entity ? `<div class="small muted">${esc(f.entity)}</div>` : "") + `</td>
        <td class="lbl">${esc(f.investment_type)}</td><td class="lbl">${esc(f.region_sector) || '<span class="muted">—</span>'}</td>
        <td title="${srcTitle(f, "vintage")}">${f.vintage || '<span class="muted">—</span>'}</td>
        <td title="${srcTitle(f, "fund_size")}">${mfmt(f.fund_size, 3)}</td>
        <td title="${srcTitle(f, "commitment")}">${mfmt(f.commitment, 3)}${estMark(f, "commitment")}</td>
        <td title="${srcTitle(f, "drawn_to_date")}">${mfmt(f.drawn_to_date, 3)}${estMark(f, "drawn_to_date")}</td>
        <td title="${srcTitle(f, "future_draw")}">${mfmt(f.future_draw, 3)}${estMark(f, "future_draw")}</td>
        <td title="${srcTitle(f, "distribution_to_date")}">${mfmt(f.distribution_to_date, 3)}${estMark(f, "distribution_to_date")}</td>
        <td>${f.drawn_commit != null ? Math.round(f.drawn_commit * 100) + "%" : '<span class="muted">—</span>'}</td>
        <td title="${srcTitle(f, "investment_balance")}"><b>${mfmt(f.investment_balance, 3)}</b>${estMark(f, "investment_balance")}</td>
        <td class="small" title="${srcTitle(f, "stats")}">${stats}</td>`;
      fb.appendChild(tr);
    });
    const cb = $("#pf-companies"); cb.innerHTML = "";
    if (!PF.companies.length)
      cb.innerHTML = '<tr><td colspan="8" class="muted" style="text-align:left">No direct investments yet.</td></tr>';
    PF.companies.forEach(c => {
      const tr = document.createElement("tr");
      tr.innerHTML = `<td class="lbl"><a href="#" class="pf-open" data-entity="${esc(c.entity)}">${esc(c.investment_name)}</a></td>
        <td class="lbl">${esc(c.theme) || '<span class="muted">—</span>'}</td>
        <td class="lbl">${esc(c.current_round) || '<span class="muted">—</span>'}</td>
        <td>${esc(c.last_investment_date || "") || '<span class="muted">—</span>'}</td>
        <td title="${srcTitle(c, "total_invested")}">${mfmt(c.total_invested)}</td>
        <td>${mfmt(c.projected_investment)}</td>
        <td title="${srcTitle(c, "total_value")}">${mfmt(c.total_value)}</td>
        <td class="lbl">${esc(c.operating_status)}${c.exited ? ' <span class="small muted">· exited</span>' : ""}</td>`;
      cb.appendChild(tr);
    });
    const t = PF.totals, tot = v => v == null ? "—" : (Number(v) < 0 ? "-" : "") + "$" + Math.abs(Number(v) / 1e6).toFixed(1) + "M";
    $("#pf-inflow").textContent = tot(t.cash_inflow);
    $("#pf-outflow").textContent = tot(t.cash_outflow);
    $("#pf-committed").textContent = tot(t.committed);
    $("#pf-net").textContent = tot(t.net_cash_flow);
    $$(".pf-open").forEach(a => a.addEventListener("click", e => { e.preventDefault(); openPortfolioEntity(a.dataset.entity); }));
    renderAttention("#pf-attention", PF.funds.concat(PF.companies).flatMap(r =>
      (r.issues || []).map(i => Object.assign({ who: r.investment_name, entity: r.entity }, i))),
      i => openPortfolioEntity(i.entity));
  }

  function openPortfolioEntity(entity) {
    const row = PF.funds.concat(PF.companies).find(r => r.entity === entity);
    if (!row) return;
    PF_ENTITY = row;
    const fields = row.kind === "fund" ? PF.fund_fields : PF.company_fields;
    $("#pf-m-title").textContent = row.investment_name;
    $("#pf-m-kind").textContent = row.kind === "fund" ? "Fund · " + entity : "Direct investment · " + entity;
    const det = row.details || {};
    const val = f => {
      const v = det[f.key];
      if (v == null) return "";
      return f.type === "money" ? Number(v).toLocaleString("en-US", { maximumFractionDigits: 2 }) : v;
    };
    const commitment = row.kind === "fund"
      ? `<label>Commitment (ours)<input name="commitment" value="${row.sources && row.sources.commitment === "fund parameters" && row.commitment != null ? Number(row.commitment).toLocaleString("en-US") : ""}" placeholder="${row.commitment != null ? Number(row.commitment).toLocaleString("en-US") + " from statements" : "e.g. 5,000,000 or 5m"}"></label>` : "";
    $("#pf-m-form").innerHTML = commitment + fields.map(f => {
      const ph = f.type === "money" ? "USD, e.g. 150,000,000 or 150m" : f.type === "date" ? "e.g. 3/15/2019"
        : f.type === "percent" ? "e.g. 4.5" : "";
      if (f.type.startsWith("choice:"))
        return `<label>${esc(f.label)}<select name="${f.key}"><option value=""></option>` +
          f.type.slice(7).split(",").map(o => `<option ${det[f.key] === o ? "selected" : ""}>${esc(o)}</option>`).join("") + "</select></label>";
      if (f.type === "longtext")
        return `<label class="wide">${esc(f.label)}<textarea name="${f.key}" rows="3">${esc(val(f))}</textarea></label>`;
      return `<label>${esc(f.label)}<input name="${f.key}" value="${esc(val(f))}" placeholder="${esc(ph)}"></label>`;
    }).join("");
    renderCalls(row);
    $("#pf-modal").classList.add("show");
    $("#pf-modal-bg").classList.add("show");
  }

  function renderCalls(row) {
    const tb = $("#pf-m-calls"); tb.innerHTML = "";
    if (!row.invested.length)
      tb.innerHTML = '<tr><td colspan="5" class="muted" style="text-align:left">None yet.</td></tr>';
    row.invested.forEach(c => {
      const tr = document.createElement("tr");
      tr.innerHTML = `<td class="lbl">${esc(c.date)}</td><td class="lbl">${esc(c.description || "-")}</td>
        <td>${mfmt(c.amount, 3)}</td><td class="lbl small muted">${esc(c.source)}</td>
        <td>${c.id ? `<button class="btn sm danger pf-del" data-id="${c.id}" aria-label="Delete this row">Delete</button>` : ""}</td>`;
      tb.appendChild(tr);
    });
    $$(".pf-del", tb).forEach(b => b.addEventListener("click", async () => {
      await fetch("/api/portfolio/calls/" + b.dataset.id, { method: "DELETE" });
      await reopenPortfolio();
    }));
  }

  async function reopenPortfolio() {
    const entity = PF_ENTITY && PF_ENTITY.entity;
    await loadPortfolio(PF.as_of);
    if (entity) openPortfolioEntity(entity);
  }

  function wireManualForms() {
    $("#dif-manual").addEventListener("submit", async e => {
      e.preventDefault();
      const f = e.target, body = Object.fromEntries(new FormData(f).entries());
      if (await saveManual(body)) f.reset();
    });
    $("#pf-add").addEventListener("submit", async e => {
      e.preventDefault();
      const f = e.target, field = n => f.elements.namedItem(n);
      const name = field("name").value.trim();
      if (!name) return;
      const res = await fetch(`/api/portfolio/${encodeURIComponent(name)}/details`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ kind: field("kind").value }) });
      if (!res.ok) { toast("Not added: " + (await res.json()).detail); return; }
      f.reset();
      await loadPortfolio(PF.as_of);
      openPortfolioEntity(name);
    });
    $("#m-add-value").addEventListener("submit", async e => {
      e.preventDefault();
      if (!CURRENT_SUB) return;
      const f = e.target, sid = CURRENT_SUB.submission.id, field = n => f.elements.namedItem(n);
      const res = await fetch(`/api/submissions/${sid}/facts`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ metric: field("metric").value, value: field("value").value }) });
      if (!res.ok) { toast("Not added: " + (await res.json()).detail); return; }
      toast("Value added — entered by you, audited");
      f.reset();
      await openSub(sid);
    });
  }

  function wirePortfolioModal() {
    const close = () => { $("#pf-modal").classList.remove("show"); $("#pf-modal-bg").classList.remove("show"); };
    $("#pf-m-close").onclick = close;
    $("#pf-modal-bg").onclick = close;
    $("#pf-m-save").onclick = async () => {
      const body = {};
      new FormData($("#pf-m-form")).forEach((v, k) => { body[k] = String(v).trim(); });
      if (PF_ENTITY.kind) body.kind = PF_ENTITY.kind;
      const res = await fetch(`/api/portfolio/${encodeURIComponent(PF_ENTITY.entity)}/details`, {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
      if (!res.ok) { toast("Not saved: " + (await res.json()).detail); return; }
      toast("Details saved (audit recorded)");
      await reopenPortfolio();
    };
    $("#pf-m-add").onclick = async () => {
      const lines = $("#pf-m-lines").value;
      if (!lines.trim()) return;
      const res = await fetch(`/api/portfolio/${encodeURIComponent(PF_ENTITY.entity)}/calls`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ lines, scale: Number($("#pf-m-scale").value) }) });
      if (!res.ok) { toast("Not added: " + (await res.json()).detail); return; }
      $("#pf-m-lines").value = "";
      toast(`Added ${(await res.json()).added} row(s)`);
      await reopenPortfolio();
    };
  }

  function pct(v, digits) {
    if (v == null) return '<span class="muted">—</span>';
    const s = (v >= 0 ? "+" : "") + v.toFixed(digits == null ? 1 : digits) + "%";
    return `<span class="${v < 0 ? "is-neg" : "is-pos"}">${s}</span>`;
  }

  function renderPerformance(perf) {
    const p = perf.portfolio || {};
    $("#perf-tvpi").textContent = p.tvpi != null ? p.tvpi.toFixed(2) + "x" : "—";
    $("#perf-dpi").textContent = p.dpi != null ? p.dpi.toFixed(2) + "x" : "—";
    $("#perf-irr").textContent = p.irr_pct != null ? p.irr_pct.toFixed(1) + "%" : "—";
    $("#perf-nav").textContent = p.total_nav != null ? money(p.total_nav) : "—";
    $("#perf-asof").textContent = p.n_funds
      ? `Across ${p.n_funds} fund${p.n_funds === 1 ? "" : "s"} · ${money(p.total_funding||0)} called · ${money(p.total_distributions||0)} distributed`
      : "No confirmed fund roll-forward data yet";

    // Direct Investments table
    const di = perf.direct_investments || [];
    const dib = $("#direct-inv-body"); dib.innerHTML = "";
    if (!di.length) {
      dib.innerHTML = '<tr><td colspan="8" class="muted" style="text-align:left">No confirmed direct-investment data yet.</td></tr>';
    }
    di.forEach(r => {
      const tr = document.createElement("tr");
      const own = r.ownership_pct == null ? '<span class="muted">—</span>' : r.ownership_pct.toFixed(1) + "%";
      const moic = r.moic == null ? '<span class="muted">—</span>' : r.moic.toFixed(2) + "x";
      tr.innerHTML = `<td class="lbl">${esc(r.company)}</td>
        <td>${esc(r.as_of_period||"")}${r.fiscal_label?`<div class="small muted fy">${esc(r.fiscal_label)}</div>`:""}</td>
        <td>${mv(r.investment_cost)}</td><td>${mv(r.estimated_fmv)}</td><td>${mv(r.book_value)}</td>
        <td class="${(r.unrealized_gain_loss||0) < 0 ? "is-neg" : "is-pos"}">${mv(r.unrealized_gain_loss)}</td>
        <td>${moic}</td><td>${own}</td>`;
      dib.appendChild(tr);
    });

  }

  let RF_FILTER = null;  // entity filter for the roll-forward table
  function renderInvestments(inv) {
    const ents = inv.entities || [], qtrs = inv.quarters || [];
    $("#inv-count").textContent =
      `${ents.length} entit${ents.length === 1 ? "y" : "ies"} · ${qtrs.length} quarter rows`;
    const et = $("#inv-entities"); et.innerHTML = "";
    if (!ents.length) {
      et.innerHTML = '<tr><td colspan="7" class="muted">No trusted investment data yet. Upload and confirm a capital-account / roll-forward file.</td></tr>';
    }
    ents.forEach(e => {
      const tr = document.createElement("tr");
      tr.className = "clickable";
      const own = e.ownership_value == null ? "" :
        (e.ownership_value < 1 ? (e.ownership_value * 100).toFixed(3) + "%" : e.ownership_value + "%");
      tr.innerHTML = `<td><b>${esc(e.entity_name)}</b></td>
        <td><span class="tag ${esc(e.entity_type)}">${esc(e.entity_type)}</span></td>
        <td class="num">${mv(e.investment_balance)}</td>
        <td class="num ${neg(e.equity_income_loss)}">${mv(e.equity_income_loss)}</td>
        <td class="num ${neg(e.return_of_capital)}">${mv(e.return_of_capital)}</td>
        <td class="num">${own}</td><td>${quarterLabel(e.latest_period)}${e.fiscal_label ? `<div class="small muted fy">${esc(e.fiscal_label)}</div>` : ""}</td>`;
      tr.onclick = () => { RF_FILTER = (RF_FILTER === e.entity_name) ? null : e.entity_name;
        renderQuarters(qtrs); };
      et.appendChild(tr);
    });
    renderQuarters(qtrs);
  }

  function renderQuarters(qtrs) {
    const rows = RF_FILTER ? qtrs.filter(q => q.entity_name === RF_FILTER) : qtrs;
    $("#rf-scope").textContent = RF_FILTER ? `— ${RF_FILTER} (click row again to clear)` : "";
    const tb = $("#inv-quarters"); tb.innerHTML = "";
    if (!rows.length) {
      tb.innerHTML = '<tr><td colspan="11" class="muted">No roll-forward quarters yet.</td></tr>'; return;
    }
    rows.forEach(q => {
      const tr = document.createElement("tr");
      const notes = q.notes || {};
      const cell = (k, bold) => {
        const n = notes[k], v = mv(q[k]);
        const badge = n ? rfBadge(n) : "";
        return `<td class="num ${neg(q[k])}">${bold ? `<b>${v}</b>` : v}${badge}</td>`;
      };
      tr.innerHTML = `<td>${esc(q.entity_name)}</td><td>${quarterLabel(q.period_label)}${q.fiscal_label ? `<div class="small muted fy">${esc(q.fiscal_label)}</div>` : ""}</td>
        ${cell("beginning_balance")}${cell("funding")}${cell("equity_income_loss")}
        ${cell("return_of_capital")}${cell("dividends")}${cell("investment_mtm")}
        ${cell("other")}${cell("change")}${cell("ending_balance", true)}`;
      tb.appendChild(tr);
    });
  }

  // "2026-Q2" -> "2026-Q2 · Apr 1–Jun 30" (calendar quarter the row covers)
  const Q_SPAN = {1: "Jan 1–Mar 31", 2: "Apr 1–Jun 30", 3: "Jul 1–Sep 30", 4: "Oct 1–Dec 31"};
  function quarterLabel(p) {
    const m = /^(20\d{2})-Q([1-4])$/.exec(p || "");
    return m ? `${esc(p)} <span class="small muted">${Q_SPAN[m[2]]}</span>` : esc(p);
  }

  // why a roll-forward value differs from what the document printed
  const RF_BADGES = {
    carried_forward: ["↺", "carried forward from the prior quarter's ending"],
    ytd_decumulated: ["YTD→Q", "year-to-date figure less the year's earlier quarters"],
    reconciling_plug: ["plug", "includes a reconciling amount so beginning + activity = ending"],
    prior_quarter_missing: ["!", "prior quarter not on file — value as printed"],
    derived_from_activity: ["Σ", "not printed — ending balance less the quarter's activity"],
  };
  function rfBadge(n) {
    const tips = (n.flags || []).map(f => (RF_BADGES[f] || [f, f])[1]);
    if (n.reported != null) tips.push(`document printed ${money(n.reported)}`);
    if (n.source) tips.push(n.source);
    const marks = (n.flags || []).map(f => (RF_BADGES[f] || [f])[0]).join(" ");
    return marks ? ` <span class="rf-badge" title="${esc(tips.join(" · "))}">${esc(marks)}</span>` : "";
  }

  const neg = v => (v != null && v < 0) ? "is-neg" : "";
  const mv = v => (v == null || v === "") ? '<span class="muted">—</span>' : money(v);

  function renderSubs(subs) {
    ENTITIES = [...new Set(subs.map(s => s.entity_name).filter(e => e && e !== "UNKNOWN"))].sort();
    const tb = $("#subs-body"); tb.innerHTML = "";
    if (!subs.length) { tb.innerHTML = '<tr><td colspan="7" class="muted">No files ingested yet. Upload above.</td></tr>'; return; }
    subs.forEach(s => {
      const tr = document.createElement("tr");
      tr.className = "clickable";
      const rt = (REPORT_TYPES[s.report_type] && REPORT_TYPES[s.report_type].label) || s.report_type;
      const status = s.status === "reviewed"
        ? '<span class="pill confirmed">reviewed</span>'
        : (s.n_review > 0 ? `<span class="pill needs_review">${s.n_review} to review</span>`
                          : '<span class="pill auto">auto ok</span>');
      tr.innerHTML = `<td>#${s.id}</td><td>${esc(s.filename)}</td><td>${esc(rt)}</td>
        <td>${esc(s.entity_name || "")}</td>
        <td>${esc(s.period || "")}${s.fiscal_label ? `<div class="small muted fy">${esc(s.fiscal_label)}</div>` : ""}</td>
        <td class="num">${s.n_facts}</td><td>${status}</td>`;
      tr.onclick = () => openSub(s.id);
      tb.appendChild(tr);
    });
  }

  // ---------------- upload ----------------
  function wireUpload() {
    const drop = $("#drop"), input = $("#file-input");
    drop.onclick = () => input.click();
    ["dragenter", "dragover"].forEach(e => drop.addEventListener(e, ev => {
      ev.preventDefault(); drop.classList.add("hover"); }));
    ["dragleave", "drop"].forEach(e => drop.addEventListener(e, ev => {
      ev.preventDefault(); drop.classList.remove("hover"); }));
    drop.addEventListener("drop", ev => uploadFiles(ev.dataTransfer.files));
    input.addEventListener("change", () => uploadFiles(input.files));
  }

  // one request per file: a model reading can take a minute or two, and each
  // request must finish within the host's time limit
  async function uploadFiles(fileList) {
    if (!fileList || !fileList.length) return;
    const files = Array.from(fileList);
    $("#upload-results").innerHTML = "";
    for (const [i, f] of files.entries()) {
      $("#upload-status").textContent =
        `Processing ${i + 1} of ${files.length}: ${f.name}… (a model reading can take a minute or two)`;
      const fd = new FormData();
      fd.append("files", f);
      let results;
      try {
        const res = await fetch("/api/upload", { method: "POST", body: fd });
        results = res.ok ? (await res.json()).results
          : [{ status: "error", filename: f.name, message: `HTTP ${res.status} — the server didn't finish this file in time; try it on its own` }];
      } catch (e) {
        results = [{ status: "error", filename: f.name, message: String(e) }];
      }
      renderUploadResults(results, true);
    }
    $("#upload-status").textContent = "";
    await refresh();
  }

  function renderUploadResults(results, append) {
    const box = $("#upload-results");
    if (!append) box.innerHTML = "";
    results.forEach(r => {
      const div = document.createElement("div");
      div.className = "result " + (r.status || "ok");
      if (r.status === "ok") {
        const errs = r.checks.filter(c => !c.ok);
        div.innerHTML = `<b>${esc(r.filename)}</b> → #${r.submission_id}
          · <span class="muted">${esc((REPORT_TYPES[r.report_type]||{}).label||r.report_type)}</span>
          · ${esc(r.entity_name)} · ${esc(r.period)}
          <br><span class="small">${r.n_facts} facts — ${r.n_auto_accepted} auto, ${r.n_needs_review} to review, ${r.n_unmapped} unmapped</span>
          ${errs.map(c => `<div class="warn-line">⚠ ${esc(c.name)}: ${esc(c.detail)}</div>`).join("")}
          ${(r.warnings||[]).map(w => `<div class="warn-line">⚠ ${esc(w)}</div>`).join("")}
          <div style="margin-top:6px"><button class="btn sm" data-open="${r.submission_id}">Review →</button></div>`;
      } else if (r.status === "duplicate") {
        div.innerHTML = `<b>${esc(r.filename)}</b> — ${esc(r.message)}`;
      } else {
        div.innerHTML = `<b>${esc(r.filename)}</b> — <span class="warn-line">${esc(r.message||"failed")}</span>`;
      }
      box.appendChild(div);
    });
    $$("#upload-results [data-open]").forEach(b =>
      b.onclick = () => openSub(Number(b.dataset.open)));
  }

  // ---------------- review modal ----------------
  let CURRENT_SUB = null;
  async function openSub(id) {
    const d = await api("/api/submissions/" + id);
    CURRENT_SUB = d;
    const s = d.submission;
    $("#m-title").textContent = `#${s.id} · ${s.filename}`;
    $("#m-meta").innerHTML =
      `<span class="badge">${esc((REPORT_TYPES[s.report_type]||{}).label||s.report_type)}</span>
       <span class="badge">${esc(s.entity_name||"")}</span>
       <span class="badge">${esc(s.period||"")}</span>
       ${s.fiscal_label ? `<span class="badge fy">${esc(s.fiscal_label)}</span>` : ""}
       <span class="badge">${esc(s.currency||"")}</span>
       <span class="badge">${esc(s.extractor)}</span>`;

    // checks (re-derive from facts flags shown; also show submission warnings)
    const warns = JSON.parse(s.warnings || "[]");
    const unmapped = JSON.parse(s.unmapped || "[]");
    $("#m-warns").innerHTML = warns.length
      ? warns.map(w => `<div class="check warn"><span class="dot"></span><span class="detail">${esc(w)}</span></div>`).join("")
      : '<div class="muted small">No submission-level warnings.</div>';

    renderPeriod(s);
    renderFileCurrency(s);
    renderFacts(d);
    const have = new Set(d.facts.filter(f => f.value != null && f.status !== "rejected").map(f => f.metric_key));
    const addable = (d.report_type_metrics || []).filter(m => !have.has(m.key));
    $("#m-add-metric").innerHTML = addable.map(m => `<option value="${m.key}">${esc(m.label)}</option>`).join("");
    $("#m-add-value").hidden = !addable.length;

    renderMemory(d);
    renderUnmapped(d, unmapped);

    $("#m-audit").innerHTML = d.audit.map(a =>
      `<div class="small mono">${a.ts.replace('T',' ')} · ${a.action}${a.field?(' · '+a.field):''}
        ${a.old_value!=null?('· '+esc(a.old_value)+' → '+esc(a.new_value)):''} ${a.note?('· '+esc(a.note)):''}</div>`
    ).join("");

    showModal(true);
  }

  // ---- format memory: what the app knows about this fund's statements ----
  function renderMemory(d) {
    const box = $("#m-memory"), m = d.memory;
    if (!m) {
      box.innerHTML = '<p class="small muted" style="margin:0">No fund identified for this file — lines taught below apply to all funds.</p>';
      return;
    }
    const q = m.quarters.length;
    const col = (m.columns || [])[0];
    box.innerHTML = `
      <div class="memory-line">
        <span><b>${esc(m.entity_name)}</b></span>
        <span>${q ? `learned from ${q} confirmed quarter${q > 1 ? "s" : ""} (${esc(m.quarters.join(", "))})`
                  : '<span class="muted">nothing confirmed yet — confirm this file to teach the app its layout</span>'}</span>
        ${col ? `<span>our column <b>“${esc(col.investor)}”</b> · quarter column <b>“${esc(col.period)}”</b></span>` : ""}
        ${m.currency ? `<span>currency ${esc(m.currency)}</span>` : ""}
        <span>${m.n_learned} remembered line${m.n_learned === 1 ? "" : "s"} · ${m.n_taught} taught term${m.n_taught === 1 ? "" : "s"}</span>
      </div>
      <details style="margin-top:6px"><summary class="small muted">Fund parameters for the model (click)</summary>
        <div class="memory-form">
          <label>How our investor appears in ${esc(m.entity_name)}'s statements
            <input id="mf-aliases" placeholder="e.g. Shareholder Q, Sample Holdings LP" value="${esc(m.profile.investor_aliases)}"></label>
          <label>Our commitment to ${esc(m.entity_name)}
            <input id="mf-commitment" placeholder="e.g. 5,000,000 — for fund-level reports" value="${esc(m.profile.commitment)}"></label>
          <label class="span2">Notes for reading this fund's statements
            <textarea id="mf-notes" placeholder="e.g. statements are year-to-date; figures in EUR">${esc(m.profile.notes)}</textarea></label>
          <div class="memory-actions">
            <button class="btn sm" id="mf-save">Save</button>
            <button class="btn sm primary" id="mf-reread" title="Save, then read this file again with the parameters">Save &amp; re-read</button>
          </div>
        </div>
      </details>`;
    const save = async () => {
      const res = await fetch(`/api/funds/${encodeURIComponent(m.entity_name)}/profile`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ investor_aliases: $("#mf-aliases").value,
                               commitment: $("#mf-commitment").value, notes: $("#mf-notes").value }) });
      if (!res.ok) { toast("Not saved: " + (await res.text())); return false; }
      return true;
    };
    $("#mf-save").onclick = async () => {
      if (await save()) toast(`Saved — used the next time a ${m.entity_name} statement is read`);
    };
    $("#mf-reread").onclick = async () => {
      if (!(await save())) return;
      toast("Re-reading the file…");
      const res = await fetch(`/api/submissions/${d.submission.id}/reread`, { method: "POST" });
      if (!res.ok) { toast("Re-read failed: " + (await res.text())); return; }
      const r = await res.json();
      toast(`Re-read: ${r.added} value(s) from the new reading · ${r.kept} reviewed value(s) kept`);
      await openSub(d.submission.id);
    };
  }

  // lines found in the file that weren't mapped: teach the app what they are
  function renderUnmapped(d, unmapped) {
    if (!unmapped.length) { $("#m-unmapped").innerHTML = ""; return; }
    const s = d.submission, m = d.memory;
    const opts = d.report_type_metrics.map(x => `<option value="${x.key}">${esc(x.label)}</option>`).join("") +
      '<option value="ignore">Not a value — skip this line</option>';
    const scope = m ? `<option value="fund">for ${esc(m.entity_name)}</option>` : "";
    $("#m-unmapped").innerHTML = `
      <details><summary class="muted small">${unmapped.length} line(s) found in the file but not mapped — teach the app what they are (click)</summary>
        <table><thead><tr><th>Line</th><th class="num">Value</th><th>Where</th><th>Teach</th></tr></thead><tbody>
        ${unmapped.map((u, i) => `<tr>
          <td>${u.section ? `<div class="small muted">${esc(u.section)}</div>` : ""}${esc(u.label)}</td>
          <td class="num">${esc(u.value_raw)}</td><td class="mono small">${esc(u.provenance)}</td>
          <td><div class="teach"><select data-teach-metric="${i}"><option value="">Field…</option>${opts}</select>
            <select data-teach-scope="${i}">${scope}<option value="all">for all funds</option></select>
            <button class="btn sm" data-teach="${i}">Teach</button></div></td></tr>`).join("")}
        </tbody></table></details>`;
    $$("#m-unmapped [data-teach]").forEach(btn => btn.onclick = async () => {
      const i = btn.dataset.teach, u = unmapped[i];
      const metric = $(`[data-teach-metric="${i}"]`).value;
      if (!metric) { toast("Pick a field first"); return; }
      const res = await fetch(`/api/submissions/${s.id}/teach`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ label: u.label, section: u.section || "", value_raw: u.value_raw,
                               metric_key: metric, scope: $(`[data-teach-scope="${i}"]`).value }) });
      if (!res.ok) { toast("Not taught: " + (await res.text())); return; }
      toast(metric === "ignore" ? "Remembered — this line will be skipped next time"
                                : "Recorded and remembered for next time");
      await openSub(s.id); loadTerms();
    });
  }

  // ---- data lake card ----
  const LAKE_STAGES = [
    ["02 Land", "land_document_registry", "documents registered"],
    ["04 Bronze", "bronze_raw_records", "lines read, with evidence"],
    ["04 Silver", "silver_financial_metric", "values, every review state"],
    ["04 Gold", "gold_fact_financial_metric", "published values"],
    ["Gold", "gold_publication", "published versions"],
  ];
  async function loadLake() {
    const st = await api("/api/lake/status");
    const card = $("#lake-card");
    card.hidden = !st.enabled;
    if (!st.enabled) return;
    $("#lake-where").textContent = `— ${st.backend} · ${st.location}`;
    $("#lake-stages").innerHTML = LAKE_STAGES.map(([stage, table, what]) => {
      const t = (st.tables || {})[table];
      return `<div class="lake-stage"><div class="small muted">${stage}</div>
        <b>${t ? t.rows.toLocaleString() : "—"}</b> <span class="small muted">${what}</span></div>`;
    }).join("");
    $("#lake-sync-line").innerHTML = (st.last_sync ? `Last sync ${esc(st.last_sync.replace("T", " ").replace("+00:00", ""))} UTC` : "Not synced yet")
      + (st.last_error ? ` · <span class="warn-line">⚠ ${esc(st.last_error)}</span>` : "");
    $("#lake-sync").onclick = async () => {
      toast("Writing Silver and Gold…");
      const res = await fetch("/api/lake/sync", { method: "POST" });
      toast(res.ok ? "Lake synced" : "Sync failed: " + (await res.text()));
      loadLake();
    };
  }

  // ---- statement terms card (pre-loaded + taught wording) ----
  let ENTITIES = [];
  async function loadTerms() {
    const t = await api("/api/terms");
    const metricLabel = Object.fromEntries(t.metrics.map(x => [x.key, x.label]));
    metricLabel.ignore = "— not a value (skip)";
    $("#t-metric").innerHTML = t.metrics.map(x => `<option value="${x.key}">${esc(x.label)}</option>`).join("") +
      '<option value="ignore">Not a value — skip</option>';
    $("#t-scope").innerHTML = '<option value="">All funds</option>' +
      ENTITIES.map(e => `<option value="${esc(e)}">${esc(e)} only</option>`).join("");
    const rows = [...t.taught, ...t.presets];
    $("#terms-count").textContent = ` — ${t.taught.length} taught · ${t.presets.length} pre-loaded`;
    $("#terms-body").innerHTML = rows.map(r => `<tr>
        <td>${r.entity_name ? esc(r.entity_name) : '<span class="muted">All funds</span>'}</td>
        <td class="small">${esc(r.section || "")}</td><td>${esc(r.label)}</td>
        <td>${esc(metricLabel[r.metric_key] || r.metric_key)}</td>
        <td><span class="tag">${r.source === "preset" ? "preset" : "taught"}</span></td>
        <td>${r.source === "preset" ? "" : `<button class="btn sm danger" data-forget="${r.id}" title="Forget this term">✕</button>`}</td></tr>`).join("");
    $$("#terms-body [data-forget]").forEach(b => b.onclick = async () => {
      await fetch(`/api/terms/${b.dataset.forget}`, { method: "DELETE" });
      toast("Term forgotten"); loadTerms();
    });
  }
  function wireTerms() {
    $("#term-form").onsubmit = async (ev) => {
      ev.preventDefault();
      const res = await fetch("/api/terms", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ label: $("#t-label").value, section: $("#t-section").value,
                               metric_key: $("#t-metric").value, entity_name: $("#t-scope").value || null }) });
      if (!res.ok) { toast("Not added: " + (await res.text())); return; }
      $("#t-label").value = ""; $("#t-section").value = "";
      toast("Term added — used from the next upload"); loadTerms();
    };
  }

  // ---- currency of a value / of a whole file ----
  const CURRENCIES = ["USD", "EUR", "GBP", "CHF", "JPY", "CNY", "HKD", "SGD", "CAD", "AUD",
                      "SEK", "NOK", "DKK", "INR", "KRW", "TWD", "ILS"];
  function currencyOptions(cur) {
    const list = !cur || CURRENCIES.includes(cur) ? CURRENCIES : [cur, ...CURRENCIES];
    return list.map(c => `<option${c === cur ? " selected" : ""}>${esc(c)}</option>`).join("");
  }
  function renderFileCurrency(s) {
    $("#p-currency").innerHTML = currencyOptions(s.currency || "USD");
    $("#p-currency-apply").onclick = async () => {
      const cur = $("#p-currency").value;
      const res = await fetch(`/api/submissions/${s.id}/currency`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ currency: cur }) });
      if (!res.ok) { toast("Not changed: " + (await res.text())); return; }
      const r = await res.json();
      toast(`File set to ${r.currency} — ${r.changed} value(s) changed (audited)`);
      await openSub(s.id);
    };
  }

  // ---- manual reporting-period correction (calendar ⇄ fiscal quarter) ----
  const FYE = window.FYE_MONTH || 9;
  const FY_START = FYE % 12 + 1;
  const MONTHS = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"];

  // same rule as db.fiscal_info: label a quarter by its first month
  function calToFiscal(y, q) {
    const m = 3 * q - 2;
    return { fy: m <= FYE ? y : y + 1, fq: Math.floor((((m - FY_START) % 12) + 12) % 12 / 3) + 1 };
  }
  function fiscalToCal(fy, fq) {
    const m = (FY_START - 1 + 3 * (fq - 1)) % 12 + 1;
    return { y: m > FYE ? fy - 1 : fy, q: Math.floor((m - 1) / 3) + 1 };
  }
  function covers(y, q) {
    const end = new Date(y, 3 * q, 0).getDate();
    return `${MONTHS[3*q-3]} 1–${MONTHS[3*q-1]} ${end}, ${y}`;
  }
  // "2026-Q2" / "2026-06-30" / "2026-06" -> {y, q}
  function parsePeriod(p) {
    let m = /^(20\d{2})-Q([1-4])$/.exec(p || "");
    if (m) return { y: +m[1], q: +m[2] };
    m = /^(20\d{2})-(\d{2})/.exec(p || "");
    if (m) return { y: +m[1], q: Math.floor((+m[2] - 1) / 3) + 1 };
    return null;
  }

  function renderPeriod(s) {
    const cur = parsePeriod(s.period) || { y: new Date().getFullYear(), q: 1 };
    const original = s.period || "";
    const calY = $("#p-cal-year"), calQ = $("#p-cal-q"), fyY = $("#p-fy-year"), fyQ = $("#p-fy-q");
    const save = $("#p-save");
    const chosen = () => `${calY.value}-${calQ.value}`;
    const sync = (from) => {
      if (from === "cal") {
        const f = calToFiscal(+calY.value, +calQ.value.slice(1));
        fyY.value = f.fy; fyQ.value = "Q" + f.fq;
      } else {
        const c = fiscalToCal(+fyY.value, +fyQ.value.slice(1));
        calY.value = c.y; calQ.value = "Q" + c.q;
      }
      $("#p-covers").textContent = calY.value
        ? `covers ${covers(+calY.value, +calQ.value.slice(1))}` +
          (original && original !== chosen() ? ` · currently ${original}` : "")
        : "";
      save.disabled = !calY.value || chosen() === original;
    };
    calY.value = cur.y; calQ.value = "Q" + cur.q; sync("cal");
    if (!parsePeriod(s.period)) save.disabled = false;  // UNKNOWN: let the reviewer set it
    calY.oninput = calQ.onchange = () => sync("cal");
    fyY.oninput = fyQ.onchange = () => sync("fy");
    save.onclick = async () => {
      const res = await fetch(`/api/submissions/${s.id}/period`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ period: chosen() }) });
      if (!res.ok) { toast("Period not saved: " + (await res.text())); return; }
      const r = await res.json();
      toast(`Period set to ${r.period} (${r.fiscal_label})`);
      await openSub(s.id);
    };
  }

  // Section order + fallback label for metric keys the current report type
  // doesn't define (e.g. after a manual re-map) — never drop a fact from view.
  const FALLBACK_CATEGORY = "other", FALLBACK_LABEL = "Other";

  function renderFacts(d) {
    const metrics = d.report_type_metrics;
    const opts = metrics.map(m => `<option value="${m.key}">${esc(m.label)}</option>`).join("");
    const catOfKey = {}, labelOfCat = {};
    metrics.forEach(m => { catOfKey[m.key] = m.category; labelOfCat[m.category] = m.category_label; });

    // group facts into sections (Assets, Income, Distributions, Ratios, Other)
    const order = (d.category_order || []).slice();
    if (!order.includes(FALLBACK_CATEGORY)) order.push(FALLBACK_CATEGORY);
    const groups = {};
    d.facts.forEach(f => {
      const cat = catOfKey[f.metric_key] || FALLBACK_CATEGORY;
      (groups[cat] = groups[cat] || []).push(f);
    });

    const tb = $("#facts-body"); tb.innerHTML = "";
    order.filter(cat => groups[cat] && groups[cat].length).forEach(cat => {
      const rows = groups[cat];
      const head = document.createElement("tr");
      head.className = "section-row";
      head.innerHTML = `<td colspan="5" class="section-head">
        ${esc(labelOfCat[cat] || FALLBACK_LABEL)}
        <span class="section-count">${rows.length}</span></td>`;
      tb.appendChild(head);

      rows.forEach(f => {
        const tr = document.createElement("tr");
        const flags = (f.flags||[]).map(x => `<span class="flag" title="validation flag">⚑ ${esc(x)}</span>`).join("");
        tr.innerHTML = `
          <td><select class="edit metric" data-id="${f.id}">${opts}</select>
              <div class="small muted mono" title="source">${esc(f.raw_label||"")} · ${esc(f.provenance||"")}</div></td>
          <td><input class="edit val" data-id="${f.id}" value="${f.value==null?'':esc(f.value)}"
               placeholder="(no value)">
               ${f.unit === "currency"
                 ? `<div class="cur-line">Currency <select class="edit cur" data-id="${f.id}"
                      title="Currency of this value">${currencyOptions(f.currency)}</select></div>`
                 : `<div class="cur-line">${esc(f.unit)}</div>`}
               ${flags?('<div>'+flags+'</div>'):''}</td>
          <td class="num small muted">${(f.confidence*100||0).toFixed(0)}%</td>
          <td><span class="pill ${f.status}">${f.status.replace('_',' ')}</span></td>
          <td>
            <button class="btn sm ok" data-confirm="${f.id}">✓</button>
            <button class="btn sm danger" data-reject="${f.id}">✕</button>
          </td>`;
        $(".metric", tr).value = f.metric_key;
        tb.appendChild(tr);
      });
    });
    // wire edits
    $$("#facts-body .val").forEach(inp => {
      inp.addEventListener("input", () => inp.classList.add("dirty"));
      inp.addEventListener("change", () => saveFact(inp.dataset.id, { value: inp.value }));
    });
    $$("#facts-body .metric").forEach(sel =>
      sel.addEventListener("change", () => saveFact(sel.dataset.id, { metric_key: sel.value })));
    $$("#facts-body .cur").forEach(sel =>
      sel.addEventListener("change", () => saveFact(sel.dataset.id, { currency: sel.value })));
    $$("#facts-body [data-confirm]").forEach(b =>
      b.onclick = () => saveFact(b.dataset.confirm, { status: "confirmed" }, true));
    $$("#facts-body [data-reject]").forEach(b =>
      b.onclick = () => saveFact(b.dataset.reject, { status: "rejected" }, true));
  }

  async function saveFact(id, patch, reopen) {
    await fetch("/api/facts/" + id, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(patch) });
    toast("Saved (audit recorded)");
    if (reopen && CURRENT_SUB) await openSub(CURRENT_SUB.submission.id);
  }

  function showModal(on) {
    $("#modal").classList.toggle("show", on);
    $("#modal-bg").classList.toggle("show", on);
    if (!on) refresh();
  }

  function wireModal() {
    $("#modal-bg").onclick = () => showModal(false);
    $("#m-close").onclick = () => showModal(false);
    $("#m-confirm-all").title = "Approve this file's values — published to Gold";
    $("#m-confirm-all").onclick = async () => {
      if (!CURRENT_SUB) return;
      await fetch(`/api/submissions/${CURRENT_SUB.submission.id}/confirm`, { method: "POST" });
      toast("Submission confirmed & locked in");
      await openSub(CURRENT_SUB.submission.id);
    };
  }

  function esc(s) {
    return String(s==null?"":s).replace(/[&<>"']/g, c =>
      ({ "&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;" }[c]));
  }

  window.addEventListener("resize", () => { /* charts redraw on refresh */ });
  document.addEventListener("DOMContentLoaded", () => {
    wireUpload(); wireModal(); wireTerms();
    $("#clear-btn").onclick = clearData;
    boot();
  });
})();
