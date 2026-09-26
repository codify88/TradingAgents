// Desk: one page, four tabs, over the JSON API in desk/app.py. No build step.
"use strict";

const TOKEN = document.querySelector('meta[name="desk-token"]').content;
const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

const state = { tab: "overview", strategy: "standard", lab: null, trading: null,
                sort: { key: "tune.t", dir: -1 }, showNulls: true, read: "" };

// --- helpers ---------------------------------------------------------------------

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;",
    '"': "&quot;", "'": "&#39;" }[c]));
}
function pct(x) { return x == null ? "—" : (x > 0 ? "+" : "") + (x * 100).toFixed(2) + "%"; }
function num(x, d = 2) { return x == null ? "—" : Number(x).toFixed(d); }
function money(x) {
  if (x == null) return "—";
  const a = Math.abs(x);
  if (a >= 1e6) return "$" + (x / 1e6).toFixed(2) + "M";
  if (a >= 1e4) return "$" + (x / 1e3).toFixed(1) + "K";
  return "$" + Number(x).toLocaleString(undefined, { maximumFractionDigits: 0 });
}
function when(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  return isNaN(d) ? esc(iso) : d.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
}
function get(obj, path) { return path.split(".").reduce((o, k) => (o == null ? o : o[k]), obj); }

async function api(path) {
  const r = await fetch(path, { headers: { Accept: "application/json" } });
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.error || `${r.status} ${r.statusText}`);
  return data;
}
async function post(path, body = {}) {
  const r = await fetch(path, { method: "POST", body: JSON.stringify(body),
    headers: { "Content-Type": "application/json", "X-Desk-Token": TOKEN } });
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.error || `${r.status} ${r.statusText}`);
  return data;
}

let toastTimer;
function toast(text, error = false) {
  const t = $("#toast");
  t.textContent = text;
  t.className = error ? "error" : "";
  t.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { t.hidden = true; }, error ? 12000 : 7000);
}

function status(kind, label) { return `<span class="status ${kind}">${esc(label)}</span>`; }

// A <dialog> as a promise: resolves to the reason text (or "") on confirm, null on cancel.
function confirmDialog({ title, body, danger = false, reason = null, ok = "Confirm" }) {
  const d = $("#confirm-dialog");
  $("#confirm-title").textContent = title;
  $("#confirm-body").innerHTML = body;
  const wrap = $("#confirm-reason-wrap");
  wrap.hidden = reason === null;
  $("#confirm-reason").value = reason || "";
  const okBtn = $("#confirm-ok");
  okBtn.textContent = ok;
  okBtn.className = danger ? "danger" : "primary";
  return new Promise(resolve => {
    d.addEventListener("close", () => resolve(d.returnValue === "confirm" ? $("#confirm-reason").value : null),
      { once: true });
    d.returnValue = "";
    d.showModal();
  });
}

// --- tabs --------------------------------------------------------------------------

const loaders = { overview: loadOverview, lab: loadLab, trading: loadTrading, decisions: async () => {} };

function show(tab) {
  state.tab = tab;
  $$("nav [role=tab]").forEach(b => b.setAttribute("aria-selected", String(b.dataset.tab === tab)));
  $$(".tab").forEach(s => { s.hidden = s.id !== `tab-${tab}`; });
  history.replaceState(null, "", `#${tab}`);
  refresh();
}

async function refresh() {
  try {
    await loaders[state.tab]();
    $("#updated").textContent = "Updated " + new Date().toLocaleTimeString([], { timeStyle: "short" });
  } catch (e) {
    toast(`Could not load ${state.tab}: ${e.message}`, true);
  }
}

// --- overview ----------------------------------------------------------------------

async function loadOverview() {
  const d = await api("/api/overview");
  $("#tab-overview").innerHTML = `
    <div class="card"><h2>This morning</h2><pre class="text">${esc(d.morning)}</pre></div>
    <div class="grid2">
      <div class="card"><h2>Lab suggestions</h2><pre class="text">${esc(d.suggestions)}</pre>
        <p><button class="small" data-goto="lab">Open the lab</button></p></div>
      <div class="card"><h2>Jobs</h2><pre class="text">${esc(d.jobs)}</pre></div>
    </div>`;
}

// --- lab ---------------------------------------------------------------------------

const COLS = [
  { key: "variant", label: "Variant", left: true },
  { key: "tune.n", label: "Tune n", fmt: v => num(v, 0) },
  { key: "tune.selection", label: "Tune sel", fmt: pct },
  { key: "tune.t", label: "Tune t", fmt: num },
  { key: "test.n", label: "Test n", fmt: v => num(v, 0) },
  { key: "test.selection", label: "Test sel", fmt: pct },
  { key: "test.t", label: "Test t", fmt: num },
  { key: "test.hit", label: "Hit", fmt: v => (v == null ? "—" : Math.round(v * 100) + "%") },
  { key: "test.net_vs_market", label: "Test net", fmt: pct },
  { key: "test.t_net", label: "Net t", fmt: num },
];

async function loadLab() {
  state.lab = await api("/api/lab");
  renderLab();
}

function renderLab() {
  const all = state.lab.strategies;
  const s = all[state.strategy];
  const res = s.results;
  const cur = s.current;
  const inUse = cur ? cur.variant : s.default;
  const verdict = res && res.verdict;

  const picker = `<div class="seg" role="group" aria-label="Strategy">${Object.keys(all).map(k =>
    `<button data-strategy="${k}" aria-pressed="${k === state.strategy}">${esc(k)}</button>`).join("")}</div>`;

  const live = cur
    ? `<b class="mono">${esc(cur.variant)}</b> — adopted ${when(cur.at)}${cur.reason ? `: “${esc(cur.reason)}”` : ""}`
    : s.default ? `<b class="mono">${esc(s.default)}</b> — the default; nothing adopted yet`
    : `the screen's built-in ordering; nothing adopted yet`;

  const wiring = s.reads_live ? "" : `<div class="banner warning"><span class="status warning">Recorded only</span>
    This strategy's live screen does not read adoptions yet. Adopting here records the choice and its
    evidence; it takes effect once the ${esc(s.name)} screen is wired to read it.</div>`;

  let verdictHtml = `<div class="banner"><span class="status">No saved results</span>
    Run <code>tradingagents lab run --strategy ${esc(state.strategy)}</code> to rank the variants here
    and adopt from them.</div>`;
  if (verdict) {
    const kind = verdict.passes ? "good" : "warning";
    const label = verdict.passes ? (verdict.tradeable ? "Clears the bar" : "Clears the bar; not yet after costs")
                                 : "No change suggested";
    verdictHtml = `<div class="banner ${kind}">${status(kind, label)}
      <p>${esc(verdict.reason)}.</p>
      <p class="muted">${verdict.trials} variants tried for ${esc(s.name)}; in-sample hurdle t ${num(verdict.hurdle)}.
        Lab run ${when(res.at)}; tuning before ${esc(res.split)}, test from it.</p></div>`;
  }

  let table = "";
  if (res) {
    const readKeys = Object.keys(res.variants[0]?.reads || {});
    const src = r => (state.read ? { ...r, tune: r.reads[state.read].tune, test: r.reads[state.read].test } : r);
    let rows = res.variants.map(src).filter(r => state.showNulls || !r.null);
    const { key, dir } = state.sort;
    rows.sort((a, b) => {
      const x = get(a, key), y = get(b, key);
      if (x == null) return 1;
      if (y == null) return -1;
      return (typeof x === "string" ? x.localeCompare(y) : x - y) * dir;
    });
    const head = COLS.map(c => `<th class="sortable ${c.left ? "left" : ""}" data-sort="${c.key}">${esc(c.label)}${
      c.key === key ? `<span class="arrow">${dir < 0 ? "▼" : "▲"}</span>` : ""}</th>`).join("");
    const body = rows.map(r => {
      const tags = [
        r.variant === inUse ? `<span class="chip accent">in use</span>` : "",
        verdict && r.variant === verdict.candidate
          ? `<span class="chip">${verdict.passes ? "lab's pick" : "best in tuning"}</span>` : "",
        r.null ? `<span class="chip">null</span>` : "",
      ].join(" ");
      const cells = COLS.map(c => c.key === "variant"
        ? `<td class="left"><span class="mono">${esc(r.variant)}</span> ${tags}</td>`
        : `<td class="num">${c.fmt(get(r, c.key))}</td>`).join("");
      const btn = r.null || r.variant === inUse || state.read ? ""
        : `<button class="small" data-adopt="${esc(r.variant)}">Adopt</button>`;
      return `<tr class="${r.null ? "null" : ""} ${r.variant === inUse ? "live" : ""}">${cells}<td>${btn}</td></tr>`;
    }).join("");
    const reads = readKeys.length ? `<label>Holding read
        <select id="read-select"><option value="">${res.horizon}-day (the verdict's)</option>${
          readKeys.map(h => `<option value="${h}" ${state.read === h ? "selected" : ""}>${h}-day (no verdict)</option>`).join("")}
        </select></label>` : "";
    table = `<div class="card">
      <div class="spread"><div><h2>Variants</h2>
        <p class="muted">Selection = picks minus the eligible pool, a holding, before costs. Net = after a
        round-trip cost, minus the S&amp;P 500. t is Newey-West. Sorted by the column you click.</p></div>
        <div class="row">${reads}<label>Nulls <select id="nulls-select">
          <option value="1" ${state.showNulls ? "selected" : ""}>shown</option>
          <option value="0" ${state.showNulls ? "" : "selected"}>hidden</option></select></label></div></div>
      ${state.read ? `<div class="banner warning"><span class="status warning">Read only</span>
        The ${state.read}-day read has no verdict; adopt from the ${res.horizon}-day view.</div>` : ""}
      <div class="table-wrap"><table><thead><tr>${head}<th></th></tr></thead><tbody>${body}</tbody></table></div>
    </div>`;
  }

  const hist = s.history.length ? `<ul class="history">${s.history.map(h => `<li>
      <b class="mono">${esc(h.variant)}</b> <span class="muted">${when(h.at)}</span>
      ${h.evidence ? (h.evidence.lab_candidate ? `<span class="chip">lab's pick</span>` : `<span class="chip">operator's call</span>`) : ""}
      <div class="secondary">${esc(h.reason || "no reason recorded")}</div></li>`).join("")}</ul>`
    : `<p class="muted">Nothing adopted for ${esc(state.strategy)} yet.</p>`;

  $("#tab-lab").innerHTML = `
    <div class="card">
      <div class="spread">
        <div><h2>${esc(s.name)}</h2>
          <p class="muted">${s.horizon}-day hold, screened every ${esc(s.every)}; style benchmark ${esc(s.style)}.</p></div>
        ${picker}
      </div>
      <p>Live screen uses: ${live}</p>
      ${s.suggestion ? `<div class="banner good"><span class="status good">Open suggestion</span>
        Adopt <b class="mono">${esc(s.suggestion.candidate)}</b>: ${esc(s.suggestion.reason)}.</div>` : ""}
    </div>
    ${wiring}
    ${verdictHtml}
    ${table}
    <div class="grid2">
      <div class="card"><h2>Adoption history</h2>${hist}</div>
      <div class="card"><h2>Full report</h2>
        ${s.report ? `<details id="report-details"><summary>The lab's report for ${esc(state.strategy)}</summary>
          <pre class="text" id="report-text">Loading…</pre></details>` : `<p class="muted">No report yet.</p>`}
      </div>
    </div>`;
}

function openAdopt(variant) {
  const s = state.lab.strategies[state.strategy];
  const r = s.results.variants.find(v => v.variant === variant);
  const verdict = s.results.verdict;
  const isPick = verdict.passes && verdict.candidate === variant;
  $("#adopt-variant").textContent = variant;
  $("#adopt-strategy").textContent = `For ${state.strategy} (${s.name}); replaces ${
    s.current ? s.current.variant : s.default || "the built-in ordering"}.`;
  const f = (label, v) => `<dt>${label}</dt><dd>${v}</dd>`;
  $("#adopt-figures").innerHTML = `<dl class="figs">
    ${f("Tune selection", pct(r.tune.selection))}${f("Tune t", num(r.tune.t))}
    ${f("Test selection", pct(r.test.selection))}${f("Test t", num(r.test.t))}
    ${f("Test net", pct(r.test.net_vs_market))}${f("Net t", num(r.test.t_net))}
    ${f("Hit rate", r.test.hit == null ? "—" : Math.round(r.test.hit * 100) + "%")}${f("Independent (test)", num(r.test.independent, 0))}
  </dl>`;
  $("#adopt-verdict").innerHTML = isPick
    ? `<div class="banner good">${status("good", "The lab's pick")} It clears the bar: ${esc(verdict.reason)}.</div>`
    : `<div class="banner warning">${status("warning", "Your call, not the lab's")}
        The lab suggests no change here — ${esc(verdict.reason)}. The adoption records that you chose it anyway;
        say why.</div>`;
  $("#adopt-live").textContent = s.reads_live
    ? "The next screen uses it. Reversible: adopt another variant at any time."
    : "Recorded only: this strategy's live screen does not read adoptions yet.";
  const form = $("#adopt-form");
  form.reason.value = "";
  const d = $("#adopt-dialog");
  d.returnValue = "";
  d.showModal();
  d.addEventListener("close", async () => {
    if (d.returnValue !== "confirm") return;
    try {
      const out = await post("/api/lab/adopt", { strategy: state.strategy, variant, reason: form.reason.value });
      toast(`Adopted ${out.adopted.variant} for ${state.strategy}` +
            (out.reads_live ? "; the next screen uses it." : "; recorded (the live screen does not read it yet)."));
      await loadLab();
    } catch (e) { toast(`Not adopted: ${e.message}`, true); }
  }, { once: true });
}

// --- trading -----------------------------------------------------------------------

async function loadTrading() {
  state.trading = await api("/api/trading");
  renderTrading();
}

function renderTrading() {
  const t = state.trading;
  const b = t.book;
  const acct = t.broker.account;

  let banner;
  if (b.halted) banner = `<div class="banner critical">${status("critical", "Halted")} ${esc(b.halted_reason || "no reason given")}. Every new order is refused.</div>`;
  else if (b.blocked.length) banner = `<div class="banner serious">${status("serious", "Blocked")} Reconciliation found mismatches nobody has acknowledged:
      <ul>${b.blocked.map(x => `<li>${esc(x)}</li>`).join("")}</ul>Check the broker, then acknowledge.</div>`;
  else banner = `<div class="banner good">${status("good", "Trading allowed")} Last reconciled ${b.last_reconciled ? when(b.last_reconciled) : "never"}.</div>`;

  const tiles = acct ? `<div class="tiles">
      <div class="tile"><div class="label">Account</div><div class="value">${acct.paper ? "Paper" : "LIVE"}</div></div>
      <div class="tile"><div class="label">Equity</div><div class="value">${money(acct.equity)}</div></div>
      <div class="tile"><div class="label">Cash</div><div class="value">${money(acct.cash)}</div></div>
      <div class="tile"><div class="label">Long market value</div><div class="value">${money(acct.long_market_value)}</div></div>
      <div class="tile"><div class="label">Live cohorts</div><div class="value">${b.live.length}</div></div>
    </div>`
    : `<div class="banner warning">${status("warning", "Broker unavailable")} ${esc(t.broker.error || "")}
        ${/key/i.test(t.broker.error || "") ? " Paper keys go in the repo's <code>.env</code>; restart Desk after adding them." : ""}</div>`;

  const actions = `<div class="row">
      <button class="danger" data-trade="halt" ${b.halted ? "disabled" : ""}>Halt trading</button>
      <button data-trade="resume" ${b.halted ? "" : "disabled"}>Resume</button>
      <button data-trade="ack" ${b.blocked.length ? "" : "disabled"}>Acknowledge mismatches</button>
      <button data-trade="reconcile" ${t.broker.available ? "" : "disabled"}>Reconcile now</button>
    </div>`;

  const p = t.pending;
  const pending = p ? `<div class="card">
      <div class="spread"><div><h2>Order plan awaiting approval</h2>
        <p class="mono">${esc(p.id)}</p>
        <p class="muted">Decisions of ${esc(p.decision_date)}, for the ${esc(p.session)} open; expires ${esc(p.expires.slice(11, 16))} New York time.
          Equity ${money(p.equity)}. Market-on-open.</p></div>
        <button class="primary" data-submit="${esc(p.id)}" ${p.orders.length && t.broker.available && !t.refusal ? "" : "disabled"}>Approve and send…</button></div>
      ${t.refusal ? `<p class="muted">Sending is refused now: ${esc(t.refusal)}.</p>` : ""}
      ${ordersTable(p.orders)}
      ${p.notes.length ? `<ul>${p.notes.map(n => `<li class="secondary">${esc(n)}</li>`).join("")}</ul>` : ""}
    </div>`
    : `<div class="card"><h2>Order plan</h2><p class="muted">No plan awaiting approval. The nightly run builds one when paper trading is enabled.</p>
        ${t.plan_error ? `<p class="muted">A plan file could not be read: ${esc(t.plan_error)}</p>` : ""}</div>`;

  const cohorts = b.live.length ? `<div class="table-wrap"><table><thead><tr><th>Entered</th><th>Exits</th><th class="left">Holdings</th><th class="left">Selling in</th></tr></thead><tbody>${
      b.live.map(c => `<tr><td>${esc(c.entry_session)}</td><td class="num">${esc(c.exit_session)}</td>
        <td class="left mono">${Object.entries(c.shares).sort().map(([s, q]) => `${esc(s)} ${q}`).join(", ")}</td>
        <td class="left mono">${esc(c.exit_plan || "")}</td></tr>`).join("")}</tbody></table></div>`
    : `<p class="muted">No live cohorts.</p>`;

  const plans = t.plans.length ? `<div class="table-wrap"><table><thead><tr><th>Plan</th><th>Status</th><th>Session</th><th>Orders</th><th>Submitted</th></tr></thead><tbody>${
      t.plans.map(x => `<tr><td class="mono">${esc(x.id)}</td><td>${esc(x.status)}</td><td class="num">${esc(x.session)}</td>
        <td class="num">${x.orders}</td><td class="num">${x.submitted ? when(x.submitted) : "—"}</td></tr>`).join("")}</tbody></table></div>`
    : `<p class="muted">No plans yet.</p>`;

  const events = t.events.length ? `<div class="table-wrap"><table><thead><tr><th>When</th><th class="left">Event</th><th class="left">Detail</th></tr></thead><tbody>${
      t.events.map(e => { const { at, event, ...rest } = e;
        return `<tr><td>${when(at)}</td><td class="left">${esc(event)}</td><td class="left mono">${esc(JSON.stringify(rest))}</td></tr>`; }).join("")}</tbody></table></div>`
    : `<p class="muted">No events yet.</p>`;

  $("#tab-trading").innerHTML = `
    ${banner}
    ${tiles}
    <div class="card"><h2>Controls</h2><p class="muted">The same actions as <code>tradingagents trade</code>, with the same refusals.</p>${actions}</div>
    ${pending}
    <div class="grid2">
      <div class="card"><h2>Live cohorts</h2>${cohorts}</div>
      <div class="card"><h2>Recent plans</h2>${plans}</div>
    </div>
    <div class="card"><h2>Events</h2>${events}</div>`;
}

function ordersTable(orders) {
  if (!orders.length) return `<p class="muted">No orders.</p>`;
  return `<div class="table-wrap"><table><thead><tr><th>Side</th><th>Qty</th><th class="left">Symbol</th><th class="left">Why</th></tr></thead><tbody>${
    orders.map(o => `<tr><td>${esc(o.side.toUpperCase())}</td><td class="num">${o.qty}</td>
      <td class="left mono">${esc(o.symbol)}</td><td class="left secondary">${esc(o.reason)}</td></tr>`).join("")}</tbody></table></div>`;
}

async function tradeAction(action) {
  const t = state.trading;
  const specs = {
    halt: { title: "Halt trading", body: "<p>Refuses every new order and cancels open ones. Positions are kept.</p>",
            danger: true, reason: "operator via Desk", ok: "Halt" },
    resume: { title: "Resume trading", body: "<p>Lifts the halt; plans can be sent again.</p>", ok: "Resume" },
    ack: { title: "Acknowledge mismatches", body: "<p>Only after checking the broker. New orders are allowed again.</p>", ok: "Acknowledge" },
    reconcile: { title: "Reconcile now", body: "<p>Books the day's fills and compares the book with the broker. Mismatches block new orders.</p>", ok: "Reconcile" },
  };
  let body = {};
  if (action === "submit") {
    const p = t.pending;
    const reason = await confirmDialog({ title: `Send ${p.orders.length} orders?`, ok: "Send orders",
      body: `<p class="mono">${esc(p.id)}</p><p>Market-on-open at the ${esc(p.session)} open, to the
        ${t.broker.account && !t.broker.account.paper ? "<b>LIVE</b>" : "paper"} account.</p>${ordersTable(p.orders)}` });
    if (reason === null) return;
    body = { plan_id: p.id };
  } else {
    const reason = await confirmDialog(specs[action]);
    if (reason === null) return;
    if (action === "halt") body = { reason };
  }
  try {
    const out = await post(`/api/trade/${action}`, body);
    toast(out.result);
  } catch (e) { toast(`${action}: ${e.message}`, true); }
  await loadTrading();
}

// --- decisions ---------------------------------------------------------------------

async function searchDecisions(form) {
  const q = new URLSearchParams([...new FormData(form)].filter(([, v]) => v));
  const out = $("#decisions-out");
  out.textContent = "Loading…";
  try { out.textContent = (await api(`/api/decisions?${q}`)).text; }
  catch (e) { out.textContent = ""; toast(e.message, true); }
}
async function openReport(form) {
  const q = new URLSearchParams(new FormData(form));
  const out = $("#report-out");
  out.textContent = "Loading…";
  try { out.textContent = (await api(`/api/decisions/report?${q}`)).text; }
  catch (e) { out.textContent = ""; toast(e.message, true); }
}

// --- wiring ------------------------------------------------------------------------

document.addEventListener("click", e => {
  const el = e.target.closest("button, th.sortable");
  if (!el) return;
  if (el.dataset.tab) return show(el.dataset.tab);
  if (el.dataset.goto) return show(el.dataset.goto);
  if (el.id === "refresh") return refresh();
  if (el.dataset.strategy) { state.strategy = el.dataset.strategy; state.read = ""; return renderLab(); }
  if (el.dataset.sort) {
    const k = el.dataset.sort;
    state.sort = { key: k, dir: state.sort.key === k ? -state.sort.dir : (k === "variant" ? 1 : -1) };
    return renderLab();
  }
  if (el.dataset.adopt) return openAdopt(el.dataset.adopt);
  if (el.dataset.trade) return tradeAction(el.dataset.trade);
  if (el.dataset.submit) return tradeAction("submit");
});

document.addEventListener("change", e => {
  if (e.target.id === "nulls-select") { state.showNulls = e.target.value === "1"; renderLab(); }
  if (e.target.id === "read-select") { state.read = e.target.value; renderLab(); }
});

document.addEventListener("toggle", async e => {
  if (e.target.id !== "report-details" || !e.target.open) return;
  try { $("#report-text").textContent = (await api(`/api/lab/${state.strategy}/report`)).report; }
  catch (err) { $("#report-text").textContent = err.message; }
}, true);

$("#decisions-form").addEventListener("submit", e => { e.preventDefault(); searchDecisions(e.target); });
$("#report-form").addEventListener("submit", e => { e.preventDefault(); openReport(e.target); });

document.addEventListener("keydown", e => {
  if (e.key === "r" && !e.metaKey && !e.ctrlKey && !/INPUT|TEXTAREA|SELECT/.test(document.activeElement.tagName)
      && !document.querySelector("dialog[open]")) refresh();
});

// Overview and trading refresh every minute (not while a dialog is open); the lab
// changes only when a lab run or an adoption does, so it refreshes on demand.
setInterval(() => {
  if (!document.hidden && !document.querySelector("dialog[open]") && ["overview", "trading"].includes(state.tab)) refresh();
}, 60_000);

show(["overview", "lab", "trading", "decisions"].includes(location.hash.slice(1)) ? location.hash.slice(1) : "overview");
