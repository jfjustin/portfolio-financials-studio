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
  }

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
        <td>${esc(s.entity_name || "")}</td><td>${esc(s.period || "")}</td>
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

  function renderFacts(d) {
    const metrics = d.report_type_metrics;
    const opts = metrics.map(m => `<option value="${m.key}">${esc(m.label)}</option>`).join("");
    const tb = $("#facts-body"); tb.innerHTML = "";
    d.facts.forEach(f => {
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
