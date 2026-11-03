// ab-connect UI. Opened from the ctrlX sidebar as /ab-connect/?token=<bearer token>.
"use strict";

const API = "/ab-connect/api/v1";
const $ = (id) => document.getElementById(id);

const token = (() => {
  const fromUrl = new URLSearchParams(location.search).get("token");
  try {
    if (fromUrl) {
      sessionStorage.setItem("ab-connect-token", fromUrl);
      history.replaceState(null, "", location.pathname);
    }
    return fromUrl || sessionStorage.getItem("ab-connect-token");
  } catch (e) {
    return fromUrl;
  }
})();

const state = { controllers: [], current: null, tags: [], checked: new Set(), treeFailed: false };

async function api(path, options = {}) {
  const res = await fetch(API + path, {
    ...options,
    headers: { "Authorization": "Bearer " + token, "Content-Type": "application/json", ...(options.headers || {}) },
  });
  const body = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(body.error || `${res.status} ${res.statusText}`);
  return body;
}

function el(tag, attrs = {}, ...children) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") e.className = v;
    else if (k.startsWith("on")) e.addEventListener(k.slice(2), v);
    else if (v !== undefined && v !== null && v !== false) e.setAttribute(k, v === true ? "" : v);
  }
  for (const c of children) e.append(c instanceof Node ? c : document.createTextNode(c ?? ""));
  return e;
}

function banner(text) {
  $("banner").textContent = text || "";
  $("banner").hidden = !text;
}

function fmtValue(v) {
  if (v === null || v === undefined) return "–";
  if (v && typeof v === "object" && "first" in v) return `[${v.first.map(fmtValue).join(", ")}, … ${v.length} elements]`;
  if (Array.isArray(v)) return `[${v.map(fmtValue).join(", ")}]`;
  if (typeof v === "string") return JSON.stringify(v);
  return String(v);
}

// --- tabs -------------------------------------------------------------------------------------

for (const tab of document.querySelectorAll(".tab")) {
  tab.addEventListener("click", () => {
    for (const t of document.querySelectorAll(".tab")) t.classList.toggle("active", t === tab);
    $("view-browse").hidden = tab.dataset.view !== "browse";
    $("view-selected").hidden = tab.dataset.view !== "selected";
  });
}

// --- controllers ------------------------------------------------------------------------------

async function loadControllers() {
  try {
    state.controllers = await api("/controllers");
    banner("");
  } catch (e) {
    banner(`Cannot reach ab-connect: ${e.message}`);
    return;
  }
  if (state.current && !state.controllers.some((c) => c.name === state.current)) state.current = null;
  if (!state.current && state.controllers.length) selectController(state.controllers[0].name);
  renderControllers();
  // the tree failed (e.g. still connecting): load it again once the PLC is connected
  const c = state.controllers.find((x) => x.name === state.current);
  if (state.treeFailed && c && c.status && c.status.connected) loadTree();
}

function controllerState(c) {
  if (!c.enabled) return ["", "disabled"];
  if (!c.status) return ["", "starting"];
  return c.status.connected ? ["ok", "connected"] : ["err", c.status.error || "not connected"];
}

function renderControllers() {
  $("controller-list").replaceChildren(...state.controllers.map((c) => {
    const [cls] = controllerState(c);
    return el("li", { class: c.name === state.current ? "active" : "", onclick: () => selectController(c.name) },
      el("span", { class: "dot " + cls }), c.name, el("span", { class: "addr" }, c.address));
  }));
  const c = state.controllers.find((x) => x.name === state.current);
  $("controller-detail").hidden = !c;
  if (!c) return;
  const [cls, text] = controllerState(c);
  $("cd-name").textContent = c.name;
  $("cd-state").textContent = text;
  $("cd-state").className = "chip push " + cls;
  $("cd-state").title = text;
  const info = (c.status && c.status.info) || {};
  const rev = info.revision ? `${info.revision.major}.${info.revision.minor}` : null;
  $("cd-info").replaceChildren(...[["Address", c.address], ["Controller", info.product_name], ["Name", info.name],
    ["Firmware", rev], ["Selected tags", c.status ? c.status.tags : null],
    ["On-request reads", c.status ? c.status.on_request_reads : null]]
    .filter(([, v]) => v !== null && v !== undefined).flatMap(([k, v]) => [el("dt", {}, k), el("dd", {}, String(v))]));
  const groups = (c.status && c.status.groups) || [];
  $("cd-groups").replaceChildren(...(groups.length ? [
    el("tr", {}, ...["Scan group", "Tags", "Last read", "Overruns"].map((h) => el("th", {}, h))),
    ...groups.map((g) => el("tr", {}, el("td", {}, `${g.rate_ms} ms`), el("td", {}, String(g.tags)),
      el("td", {}, g.last_ms === null ? "–" : `${g.last_ms} ms`),
      el("td", { class: g.overruns ? "over" : "", title: "Reads that took longer than the scan period" }, String(g.overruns)))),
  ] : []));
  $("cd-toggle").textContent = c.enabled ? "Disable" : "Enable";
}

function selectController(name) {
  if (state.current === name) return;
  state.current = name;
  state.checked.clear();
  $("sel-controller").textContent = name;
  $("preview").replaceChildren("Click the eye icon on a tag to read its current value.");
  renderControllers();
  loadTree();
  loadTags();
}

$("add-controller").addEventListener("click", () => {
  $("controller-form").hidden = false;
  $("cf-name").focus();
});
$("cf-cancel").addEventListener("click", () => { $("controller-form").hidden = true; $("discovered").replaceChildren(); });
$("controller-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  try {
    await api("/controllers", { method: "POST", body: JSON.stringify({ name: $("cf-name").value.trim(), address: $("cf-address").value.trim() }) });
    const name = $("cf-name").value.trim();
    $("controller-form").reset();
    $("controller-form").hidden = true;
    $("discovered").replaceChildren();
    await loadControllers();
    selectController(name);
  } catch (err) {
    alert(err.message);
  }
});
async function discover(range) {
  $("searched").textContent = "";
  $("discovered").replaceChildren(el("li", {}, range ? `Asking every address in ${range}…` : "Searching all networks…"));
  for (const b of [$("cf-discover"), $("cf-scan")]) b.disabled = true;
  try {
    const r = await api("/discover" + (range ? `?range=${encodeURIComponent(range)}` : ""));
    $("searched").textContent = "Searched: " + (r.searched.length
      ? r.searched.map((s) => s.range || `${s.network} (${s.interface})`).join(", ") : "the default network");
    $("discovered").replaceChildren(...(r.devices.length ? r.devices.map((d) => el("li", {
      title: "Use this address",
      onclick: () => {
        $("cf-address").value = d.ip_address;
        if (!$("cf-name").value) $("cf-name").value = (d.product_name || "PLC").replace(/[^A-Za-z0-9_-]/g, "_").replace(/^[^A-Za-z]+/, "PLC_");
      },
    }, `${d.ip_address} – ${d.product_name || "?"}`)) : [el("li", {}, range
      ? "No devices answered." : "No devices answered. If the PLC is behind a router or the ctrlX is virtual, scan its range.")]));
  } catch (err) {
    $("discovered").replaceChildren(el("li", {}, err.message));
  } finally {
    for (const b of [$("cf-discover"), $("cf-scan")]) b.disabled = false;
  }
}
$("cf-discover").addEventListener("click", () => discover(null));
$("cf-scan").addEventListener("click", () => {
  const range = $("cf-range").value.trim();
  if (range) discover(range); else $("cf-range").focus();
});
$("cd-refresh").addEventListener("click", async () => {
  try {
    await api(`/controllers/${encodeURIComponent(state.current)}/refresh`, { method: "POST" });
    loadTree();
  } catch (err) {
    alert(err.message);
  }
});
$("cd-toggle").addEventListener("click", async () => {
  const c = state.controllers.find((x) => x.name === state.current);
  await api(`/controllers/${encodeURIComponent(c.name)}`, { method: "PATCH", body: JSON.stringify({ enabled: !c.enabled }) })
    .catch((err) => alert(err.message));
  loadControllers();
});
$("cd-remove").addEventListener("click", async () => {
  const n = state.tags.length;
  if (!confirm(`Remove ${state.current}${n ? ` and its ${n} selected tags (their Data Layer nodes disappear)` : ""}?`)) return;
  await api(`/controllers/${encodeURIComponent(state.current)}`, { method: "DELETE" }).catch((err) => alert(err.message));
  state.current = null;
  $("tree").replaceChildren(el("p", { class: "hint" }, "Choose a controller."));
  loadControllers();
  loadTags();
});

// --- tag tree ---------------------------------------------------------------------------------

// An eye icon as SVG (emoji fonts aren't available everywhere)
function eyeIcon() {
  const ns = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(ns, "svg");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("width", "16");
  svg.setAttribute("height", "16");
  svg.setAttribute("aria-hidden", "true");
  for (const [tag, attrs] of [["path", { d: "M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7S2 12 2 12z" }], ["circle", { cx: "12", cy: "12", r: "3" }]]) {
    const e = document.createElementNS(ns, tag);
    for (const [k, v] of Object.entries(attrs)) e.setAttribute(k, v);
    e.setAttribute("fill", "none");
    e.setAttribute("stroke", "currentColor");
    e.setAttribute("stroke-width", "2");
    svg.append(e);
  }
  return svg;
}

function typeText(item) {
  if (item.kind === "scope") return "";
  if (item.dims) return `${item.type}[${item.dims.join(",")}]`;
  if (item.bit !== null && item.bit !== undefined) return `${item.type} (bit ${item.bit})`;
  return item.type;
}

function isAdded(item) {
  return state.tags.some((t) => t.scope === item.scope && (t.path === item.path || t.path.startsWith(item.path + ".") || t.path.startsWith(item.path + "[")));
}

async function fetchChildren(scope, path, offset = 0) {
  const q = new URLSearchParams({ scope: scope || "", path: path || "", offset, limit: 100 });
  return api(`/controllers/${encodeURIComponent(state.current)}/browse?${q}`);
}

function nodeRow(item) {
  const li = el("li");
  const childList = el("ul", { hidden: true });
  let loaded = false;
  const toggle = el("span", { class: "toggle" }, item.has_children ? "▸" : "");
  async function expand() {
    if (!item.has_children) return;
    if (!childList.hidden) {
      childList.hidden = true;
      toggle.textContent = "▸";
      return;
    }
    childList.hidden = false;
    toggle.textContent = "▾";
    if (!loaded) {
      loaded = true;
      await loadInto(childList, item.scope, item.path, 0);
    }
  }
  toggle.addEventListener("click", expand);
  const actions = el("span", { class: "actions" });
  if (item.kind !== "scope") {
    actions.append(
      el("button", { type: "button", class: "icon", title: "Read the current value", onclick: () => preview(item) }, eyeIcon()),
      el("button", { type: "button", class: "icon", title: item.kind === "struct" || (item.kind === "array" && !item.dl_type)
        ? "Add all values in this structure" : "Add to the Data Layer", onclick: (e) => addTag(item, e.target) }, "＋"));
  }
  const row = el("div", { class: "node", ondblclick: expand }, toggle, el("span", { class: "name" }, item.name),
    el("span", { class: "type" }, typeText(item)), isAdded(item) ? el("span", { class: "added", title: "Selected" }, "✓") : "", actions);
  li.append(row, childList);
  return li;
}

async function loadInto(ul, scope, path, offset) {
  const loading = el("li", { class: "hint" }, "Loading…");
  ul.append(loading);
  try {
    const result = await fetchChildren(scope, path, offset);
    loading.remove();
    if (!result.items.length && offset === 0) ul.append(el("li", { class: "hint" }, "(empty)"));
    for (const item of result.items) ul.append(nodeRow(item));
    const shown = (result.offset || 0) + result.items.length;
    if (shown < result.total) {
      const more = el("li", { class: "more" }, el("button", { type: "button", class: "small secondary", onclick: async () => {
        more.remove();
        await loadInto(ul, scope, path, shown);
      } }, `Show elements ${shown}–${Math.min(shown + 100, result.total) - 1} of ${result.total}`));
      ul.append(more);
    }
  } catch (e) {
    if (ul.parentElement === $("tree")) state.treeFailed = true;
    loading.replaceChildren(e.message + " ",
      el("button", { type: "button", class: "small secondary", onclick: () => { loading.remove(); loadInto(ul, scope, path, offset); } }, "Retry"));
    loading.className = "hint";
  }
}

function loadTree() {
  state.treeFailed = false;
  const root = el("ul");
  $("tree").replaceChildren(root);
  if (!state.current) return;
  loadInto(root, "", "", 0);
}

async function preview(item) {
  $("preview").replaceChildren(el("p", { class: "hint" }, `Reading ${item.path}…`));
  try {
    const q = new URLSearchParams({ scope: item.scope, path: item.path });
    const r = await api(`/controllers/${encodeURIComponent(state.current)}/preview?${q}`);
    if (!r.values) {
      $("preview").replaceChildren(el("p", { class: "hint" }, r.note));
      return;
    }
    $("preview").replaceChildren(el("p", {}, el("strong", {}, item.path)), ...r.values.map((v) => el("div", {},
      el("div", { class: "type" }, `${v.path} · ${v.type}${v.count ? `[${v.count}]` : ""}`),
      v.error ? el("pre", { class: "err" }, v.error)
        : el("pre", {}, fmtValue(v.value) + (v.count && v.count > (v.value || []).length ? ` … (${v.count} elements)` : "")))));
  } catch (e) {
    $("preview").replaceChildren(el("p", { class: "err" }, e.message));
  }
}

async function addTag(item, button) {
  const rate = Number($("opt-rate").value);
  button.disabled = true;
  try {
    const r = await api("/tags", { method: "POST", body: JSON.stringify({
      controller: state.current, scope: item.scope, path: item.path, mode: $("opt-mode").value,
      rate_ms: rate, writable: $("opt-writable").checked }) });
    const what = r.added === 1 ? r.tags[0].node : `${r.added} values`;
    button.textContent = "✓";
    button.title = `Added ${what}${r.skipped ? ` (${r.skipped} were already selected)` : ""}`;
    await loadTags();
    const row = button.closest(".node");
    if (row && !row.querySelector(".added")) row.insertBefore(el("span", { class: "added", title: "Selected" }, "✓"), row.querySelector(".actions"));
  } catch (e) {
    alert(e.message);
    button.disabled = false;
  }
}

$("opt-rate").addEventListener("input", () => { $("rate-warning").hidden = Number($("opt-rate").value) >= 100; });

// --- selected tags ----------------------------------------------------------------------------

async function loadTags() {
  if (!state.current) {
    state.tags = [];
  } else {
    try {
      state.tags = await api(`/tags?controller=${encodeURIComponent(state.current)}`);
    } catch (e) {
      return;
    }
  }
  $("selected-count").textContent = state.tags.length ? `(${state.tags.length})` : "";
  renderTags();
}

let editing = false;   // don't redraw the table while the user is typing in it
let renderedSignature = null;

// The table is rebuilt only when the selection itself changes; the 1 s refresh just updates the
// value cells. (Rebuilding every second closed any open drop-down and dropped focus.)
function tableSignature() {
  return JSON.stringify(state.tags.map(({ value, error, age_ms, ...config }) => config));
}

function renderTags(force = false) {
  const signature = tableSignature();
  if (editing || (!force && signature === renderedSignature)) return updateValues();
  renderedSignature = signature;
  const head = el("tr", {}, el("th", {}, el("input", { type: "checkbox", title: "Check all", onchange: (e) => {
    state.checked = e.target.checked ? new Set(state.tags.map((t) => t.node)) : new Set();
    renderTags(true);
  } })), ...["Data Layer node", "PLC tag", "Type", "Mode", "Rate (ms)", "Writable", "Value"].map((h) => el("th", {}, h)));
  const rows = state.tags.map((t) => {
    const save = async (changes) => {
      try {
        await api("/tags", { method: "PATCH", body: JSON.stringify({ controller: t.controller, node: t.node, changes }) });
      } catch (e) {
        alert(e.message);
      }
      editing = false;
      loadTags();
    };
    const rate = el("input", { type: "number", min: "50", step: "10", value: t.rate_ms,
      onfocus: () => { editing = true; }, onchange: (e) => save({ rate_ms: Number(e.target.value) }), onblur: () => { editing = false; } });
    return el("tr", { "data-node": t.node },
      el("td", {}, el("input", { type: "checkbox", checked: state.checked.has(t.node), onchange: (e) => {
        if (e.target.checked) state.checked.add(t.node); else state.checked.delete(t.node);
        $("remove-selected").disabled = !state.checked.size;
      } })),
      el("td", { class: "mono" }, t.node),
      el("td", { class: "mono" }, (t.scope === "controller" ? "" : `Program:${t.scope.slice(8)}.`) + t.path),
      el("td", {}, t.count ? `${t.type}[${t.count}]` : t.type),
      el("td", {}, el("select", { onchange: (e) => save({ mode: e.target.value }) },
        el("option", { value: "cyclic", selected: t.mode === "cyclic" }, "Cyclic"),
        el("option", { value: "on-request", selected: t.mode === "on-request" }, "On request"))),
      el("td", {}, rate),
      el("td", {}, el("input", { type: "checkbox", checked: t.writable, onchange: (e) => save({ writable: e.target.checked }) })),
      valueCell(t));
  });
  $("tag-table").replaceChildren(el("thead", {}, head), el("tbody", {}, ...rows));
  $("remove-selected").disabled = !state.checked.size;
}

function valueCell(t) {
  if (t.error) return el("td", { class: "bad", title: t.error }, t.error);
  if (t.mode === "on-request" && t.value === null) return el("td", { class: "hint" }, "read when requested");
  const text = fmtValue(t.value);
  return el("td", { class: "val", title: text + (t.age_ms !== null ? ` (${t.age_ms} ms old)` : "") }, text);
}

function updateValues() {
  for (const t of state.tags) {
    const row = $("tag-table").querySelector(`tr[data-node="${CSS.escape(t.node)}"]`);
    if (row) row.lastChild.replaceWith(valueCell(t));
  }
}

$("remove-selected").addEventListener("click", async () => {
  const nodes = [...state.checked];
  if (!confirm(`Remove ${nodes.length} tag(s)? Their Data Layer nodes disappear.`)) return;
  try {
    await api("/tags/delete", { method: "POST", body: JSON.stringify({ controller: state.current, nodes }) });
  } catch (e) {
    alert(e.message);
  }
  state.checked.clear();
  await loadTags();
  loadTree();
});

// --- resizable controller pane ----------------------------------------------------------------

(() => {
  const MIN = 200, MAX = 600, DEFAULT = 290, KEY = "ab-connect-sidebar-width";
  const layout = document.querySelector(".layout");
  const handle = $("resizer");
  const setWidth = (w, save = true) => {
    w = Math.round(Math.min(MAX, Math.max(MIN, w)));
    layout.style.setProperty("--sidebar-width", `${w}px`);
    handle.setAttribute("aria-valuenow", String(w));
    if (save) {
      try { localStorage.setItem(KEY, String(w)); } catch (e) { /* storage unavailable: just don't remember */ }
    }
  };
  try {
    const saved = Number(localStorage.getItem(KEY));
    if (saved) setWidth(saved, false);
  } catch (e) { /* ignore */ }
  handle.addEventListener("pointerdown", (e) => {
    e.preventDefault();
    try { handle.setPointerCapture(e.pointerId); } catch (err) { /* keep dragging without capture */ }
    handle.classList.add("dragging");
    document.body.classList.add("resizing");
    // keep the point where the handle was grabbed under the pointer
    const left = layout.getBoundingClientRect().left + (e.clientX - handle.getBoundingClientRect().left);
    const move = (ev) => setWidth(ev.clientX - left, false);
    const up = (ev) => {
      try { handle.releasePointerCapture(ev.pointerId); } catch (err) { /* not captured */ }
      handle.removeEventListener("pointermove", move);
      handle.removeEventListener("pointerup", up);
      handle.classList.remove("dragging");
      document.body.classList.remove("resizing");
      setWidth(ev.clientX - left);
    };
    handle.addEventListener("pointermove", move);
    handle.addEventListener("pointerup", up);
  });
  handle.addEventListener("dblclick", () => setWidth(DEFAULT));
  handle.addEventListener("keydown", (e) => {
    const current = parseInt(getComputedStyle(layout).getPropertyValue("--sidebar-width"), 10) || DEFAULT;
    if (e.key === "ArrowLeft") setWidth(current - 20);
    else if (e.key === "ArrowRight") setWidth(current + 20);
    else return;
    e.preventDefault();
  });
})();

// --- start ------------------------------------------------------------------------------------

if (!token) banner("No login token. Open ab-connect from the ctrlX sidebar so it can act on your behalf.");
loadControllers();
setInterval(() => {
  if (document.hidden) return;
  loadControllers();
  if (!$("view-selected").hidden) loadTags();
}, 1000);
