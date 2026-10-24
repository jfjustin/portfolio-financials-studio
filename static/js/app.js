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
    else if (ps.available) { b.textContent = h.llm_provider + " ✓"; b.className = "badge on"; }
    else { b.textContent = h.llm_provider + " unavailable"; b.className = "badge off"; }
    if (ps.detail) b.title = ps.detail;
    $("#ledger-filter").addEventListener("input", renderLedger);
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

    // Direct Investments — Operating Metrics table
    const om = perf.operating_metrics || [];
    const omb = $("#op-metrics-body"); omb.innerHTML = "";
    if (!om.length) {
      omb.innerHTML = '<tr><td colspan="8" class="muted" style="text-align:left">No confirmed operating-metrics data yet.</td></tr>';
    }
    om.forEach(r => {
      const tr = document.createElement("tr");
      tr.innerHTML = `<td class="lbl">${esc(r.company)}</td>
        <td>${esc(r.as_of_period||"")}${r.fiscal_label?`<div class="small muted fy">${esc(r.fiscal_label)}</div>`:""}</td>
        <td>${mv(r.revenue)}</td>
        <td class="${(r.operating_cash_flow||0) < 0 ? "is-neg" : "is-pos"}">${mv(r.operating_cash_flow)}</td>
        <td>${mv(r.capex)}</td><td>${mv(r.ending_cash)}</td><td>${mv(r.operating_expense)}</td>
        <td class="${(r.net_burn||0) > 0 ? "is-neg" : "is-pos"}">${mv(r.net_burn)}</td>`;
      omb.appendChild(tr);
    });

    // Fund Investments table
    const fd = perf.funds || [];
    const fib = $("#fund-inv-body"); fib.innerHTML = "";
    if (!fd.length) {
      fib.innerHTML = '<tr><td colspan="8" class="muted" style="text-align:left">No confirmed fund roll-forward data yet.</td></tr>';
    }
    fd.forEach(r => {
      const tr = document.createElement("tr");
      tr.innerHTML = `<td class="lbl">${esc(r.fund)}</td>
        <td>${esc(r.since||"")}</td><td>${mv(r.commitment)}</td>
        <td>${mv(r.cumulative_funding)}</td><td>${mv(r.cumulative_distributions)}</td>
        <td><b>${mv(r.lam_investment_balance)}</b>${r.fiscal_label?`<div class="small muted fy">${esc(r.fiscal_label)}</div>`:""}</td>
        <td class="${(r.qoq_change||0) < 0 ? "is-neg" : "is-pos"}">${mv(r.qoq_change)}</td>
        <td>${pct(r.total_pct_gain_loss)}</td>`;
      fib.appendChild(tr);
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

  async function uploadFiles(fileList) {
    if (!fileList || !fileList.length) return;
    const fd = new FormData();
    for (const f of fileList) fd.append("files", f);
    $("#upload-status").textContent = `Processing ${fileList.length} file(s)…`;
    const res = await api("/api/upload", { method: "POST", body: fd });
    $("#upload-status").textContent = "";
    renderUploadResults(res.results);
    await refresh();
  }

  function renderUploadResults(results) {
    const box = $("#upload-results"); box.innerHTML = "";
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

    renderFacts(d);

    $("#m-unmapped").innerHTML = unmapped.length
      ? `<details><summary class="muted small">${unmapped.length} unmapped line(s) found in the file (click)</summary>
         <table><thead><tr><th>Label</th><th>Value</th><th>Where</th><th></th></tr></thead><tbody>
         ${unmapped.map((u,i) => `<tr><td>${esc(u.label)}</td><td class="num">${esc(u.value_raw)}</td>
           <td class="mono">${esc(u.provenance)}</td></tr>`).join("")}
         </tbody></table></details>`
      : "";

    $("#m-audit").innerHTML = d.audit.map(a =>
      `<div class="small mono">${a.ts.replace('T',' ')} · ${a.action}${a.field?(' · '+a.field):''}
        ${a.old_value!=null?('· '+esc(a.old_value)+' → '+esc(a.new_value)):''} ${a.note?('· '+esc(a.note)):''}</div>`
    ).join("");

    showModal(true);
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
               placeholder="(no value)"> <span class="small muted">${esc(f.unit)} ${esc(f.currency)}</span>
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
    wireUpload(); wireModal();
    $("#refresh-btn").onclick = refresh;
    boot();
  });
})();
