"use strict";
(function () {
  const $ = (id) => document.getElementById(id);
  const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  const tok = (n) => { n = +n || 0; for (const [u, d] of [["B", 1e9], ["M", 1e6], ["K", 1e3]]) if (n >= d) return (n / d).toFixed(n / d >= 100 ? 0 : 1) + u; return String(n); };
  const bytes = (n) => { n = +n || 0; for (const [u, d] of [["GB", 1e9], ["MB", 1e6], ["KB", 1e3]]) if (n >= d) return (n / d).toFixed(n / d >= 100 ? 0 : 1) + " " + u; return n + " B"; };
  const pct = (x, d = 1) => (x * 100).toFixed(d) + "%";
  const int = (n) => (+n || 0).toLocaleString("en-US");
  const css = (v) => getComputedStyle(document.documentElement).getPropertyValue(v).trim();
  const dayTs = (d) => Date.parse(d + "T00:00:00Z") / 1000;
  const ago = (iso) => { const m = Math.round((Date.now() - Date.parse(iso)) / 60000); return m < 60 ? m + " min ago" : m < 2880 ? Math.round(m / 60) + " h ago" : Math.round(m / 1440) + " days ago"; };

  const STATIC = window.OBS_DATA || null;   // set by report.html: one UTC day, data inlined
  // hash: "#14" or "#14&harness=codex,kimi-code" (range, then optional harness filter)
  const hp = location.hash.slice(1).split("&");
  let days = +hp[0] || 14;
  if (![7, 14, 30].includes(days)) days = 14;
  let hsel = ((hp.find((x) => x.startsWith("harness=")) || "").slice(8).split(",").map(decodeURIComponent)).filter(Boolean);
  const setHash = () => history.replaceState(null, "", "#" + days + (hsel.length ? "&harness=" + hsel.map(encodeURIComponent).join(",") : ""));
  const hq = () => hsel.map((h) => "&harness=" + encodeURIComponent(h)).join("");
  const HCOL = ["#1f4e79", "#b4531a", "#2a7f62", "#7a3e9d", "#8a7a1a", "#a02c4a"];
  const hcolor = (name, all) => HCOL[Math.max(0, all.indexOf(name)) % HCOL.length];
  let allH = [];
  const htag = (h) => h ? `<span class="tag">${esc(h)}</span>` : "";
  const plots = {};
  let cache = {};

  async function get(name) {
    if (STATIC) { if (!STATIC[name]) throw new Error(name + " missing"); return STATIC[name]; }
    const r = await fetch(`/api/${name}?days=${days}${hq()}`);
    if (!r.ok) throw new Error(`${name}: ${r.status} ${(await r.text()).slice(0, 160)}`);
    return r.json();
  }

  function table(el, cols, rows) {
    if (!rows.length) { el.innerHTML = '<p class="empty">Nothing recorded for this range.</p>'; return; }
    const h = cols.map((c) => `<th class="${c.num ? "num" : ""}">${esc(c.label)}</th>`).join("");
    const b = rows.map((r) => "<tr>" + cols.map((c) => {
      const v = c.fmt ? c.fmt(r) : esc(r[c.key]);
      return `<td class="${c.num ? "num" : ""} ${c.cls || ""}" ${c.title ? `title="${esc(c.title(r))}"` : ""}>${v}</td>`;
    }).join("") + "</tr>").join("");
    el.innerHTML = `<table><thead><tr>${h}</tr></thead><tbody>${b}</tbody></table>`;
  }

  function bars(el, rows, label, value, fmt, cool) {
    if (!rows.length) { el.innerHTML = '<p class="empty">Nothing recorded for this range.</p>'; return; }
    const max = Math.max(...rows.map(value)) || 1;
    el.innerHTML = rows.map((r) => `<div class="brow"><span class="n" title="${esc(label(r))}">${esc(label(r))}</span>` +
      `<span class="track"><span class="fill ${cool ? "cool" : ""}" style="width:${(100 * value(r) / max).toFixed(1)}%;display:block"></span></span>` +
      `<span class="v">${fmt(r)}</span></div>`).join("");
  }

  const xsOf = (rows) => rows.map((r) => (r.t != null ? r.t : dayTs(r.day)));
  const stepOf = (d) => (d.bucket === "hour" ? 3600 : 86400);

  function axes(fmtY, bucket) {
    const ink = css("--muted"), grid = css("--rule");
    const hourly = bucket === "hour";
    return [
      { stroke: ink, grid: { show: false }, ticks: { stroke: grid }, font: "11px " + css("--mono"),
        incrs: hourly ? [3600, 2 * 3600, 3 * 3600, 6 * 3600] : [86400, 2 * 86400, 3 * 86400, 7 * 86400],
        values: (u, vals) => vals.map((v) => { const d = new Date(v * 1000);
          return hourly ? String(d.getUTCHours()).padStart(2, "0") + ":00" : `${d.getUTCMonth() + 1}/${d.getUTCDate()}`; }) },
      { stroke: ink, grid: { stroke: grid, width: 1 }, ticks: { show: false }, font: "11px " + css("--mono"), size: 56,
        values: (u, vals) => vals.map(fmtY) },
    ];
  }

  function plot(key, el, opts, data) {
    if (plots[key]) plots[key].destroy();
    el.innerHTML = "";
    const w = el.clientWidth || 600;
    plots[key] = new uPlot(Object.assign({ width: w, height: 230, legend: { show: false },
      cursor: { points: { show: false }, drag: { x: false, y: false } } }, opts), data, el);
  }

  // days before the first recorded event: hatched band + "no data" label (never drawn as zero)
  function noDataHook(rows, half) {
    const nd = rows.filter((r) => r.no_data);
    return (u) => { if (!nd.length) return;
      const x0 = u.valToPos(nd[0].t - half, "x", true), x1 = u.valToPos(nd[nd.length - 1].t + half, "x", true);
      const c = u.ctx, dpr = devicePixelRatio, L = Math.max(x0, u.bbox.left), R = Math.min(x1, u.bbox.left + u.bbox.width);
      if (R <= L) return;
      c.save(); c.beginPath(); c.rect(L, u.bbox.top, R - L, u.bbox.height); c.clip();
      c.fillStyle = css("--rule") || "#8884"; c.globalAlpha = 0.35; c.fillRect(L, u.bbox.top, R - L, u.bbox.height);
      c.globalAlpha = 0.6; c.strokeStyle = css("--muted") || "#888"; c.lineWidth = dpr;
      for (let x = L - u.bbox.height; x < R; x += 10 * dpr) { c.beginPath(); c.moveTo(x, u.bbox.top + u.bbox.height); c.lineTo(x + u.bbox.height, u.bbox.top); c.stroke(); }
      c.restore();
      let tag = u.over.querySelector(".nodata"); if (!tag) { tag = document.createElement("div"); tag.className = "nodata"; tag.textContent = "no data"; u.over.appendChild(tag); }
      tag.style.left = Math.max(0, (L - u.bbox.left) / dpr + 6) + "px"; };
  }

  // dashed horizontal reference line at value y, labelled at the right edge of the plot
  function medianHook(y, label) {
    return (u) => { if (y == null) return;
      const c = u.ctx, dpr = devicePixelRatio, py = Math.round(u.valToPos(y, "y", true));
      if (py < u.bbox.top || py > u.bbox.top + u.bbox.height) return;
      c.save(); c.strokeStyle = css("--ink") || "#888"; c.globalAlpha = 0.75; c.lineWidth = 1.5 * dpr;
      c.setLineDash([6 * dpr, 4 * dpr]); c.beginPath(); c.moveTo(u.bbox.left, py); c.lineTo(u.bbox.left + u.bbox.width, py); c.stroke();
      c.setLineDash([]); c.globalAlpha = 1; c.font = `600 ${11 * dpr}px ${css("--mono")}`; c.textAlign = "right"; c.textBaseline = "bottom";
      const tx = u.bbox.left + u.bbox.width - 4 * dpr, ty = py - 4 * dpr, tw = c.measureText(label).width;
      c.fillStyle = css("--panel") || "#000"; c.fillRect(tx - tw - 3 * dpr, ty - 13 * dpr, tw + 6 * dpr, 14 * dpr);
      c.fillStyle = css("--ink") || "#888"; c.fillText(label, tx, ty); c.restore(); };
  }

  function drawContext(d) {
    const rows = d.rows, xs = xsOf(rows), half = stepOf(d) / 2;
    const ctx = rows.map((r) => r.context), tot = rows.map((r) => (r.no_data ? null : r.context + r.output));
    const bar = uPlot.paths.bars({ size: [0.72, 48], align: 0 });
    const ro = $("r-context");
    const key = `<span class="key"><span><i class="sw" style="background:${css("--accent")}"></i>context (input + cache read)</span>` +
      `<span><i class="sw" style="background:${css("--cool")}"></i>output</span>` +
      (d.median_context != null ? `<span><i class="sw dash"></i>median day</span>` : "") + `</span>`;
    ro.innerHTML = key;
    plot("context", $("c-context"), {
      height: 300,
      scales: { x: { time: true, range: (u, a, b) => [a - half, b + half] }, y: { range: (u, a, b) => [0, b * 1.05] } },
      axes: axes(tok, d.bucket),
      series: [{}, { label: "output", fill: css("--cool"), stroke: css("--cool"), paths: bar, width: 0, points: { show: false } },
                   { label: "context", fill: css("--accent"), stroke: css("--accent"), paths: bar, width: 0, points: { show: false } }],
      hooks: { draw: [noDataHook(rows, half), medianHook(d.median_context, "median " + tok(d.median_context))],
        setCursor: [(u) => { const i = u.cursor.idx; if (i == null) { ro.innerHTML = key; return; }
        const r = rows[i]; if (r.no_data) { ro.textContent = `${r.day}  no data (before the first recorded event)`; return; }
        ro.textContent = `${r.day}  context ${int(r.context)}  output ${int(r.output)}  requests ${int(r.requests)}`; }] },
    }, [xs, tot, ctx]);
    const sum = d.total_context, peak = rows.filter((r) => !r.no_data).reduce((a, r) => (r.context > a.context ? r : a), { context: 0 });
    $("f-context").innerHTML = `<b>${tok(sum)}</b> <span>context tokens, ${tok(d.total_output)} output; peak ${esc(peak.day || "")} at ${tok(peak.context)}</span>`;
  }

  function drawCache(d) {
    const rows = d.rows;
    const xs = xsOf(rows), p50 = rows.map((r) => r.p50), p95 = rows.map((r) => r.p95), q = stepOf(d) / 4;
    const per = d.bucket === "hour" ? "hour" : "day", span = d.bucket === "hour" ? "day" : `${days}-day`;
    $("r-cache").innerHTML = `<span class="key"><span><i class="sw" style="background:${css("--cool")}"></i>median per ${per}</span>` +
      `<span><i class="sw" style="background:${css("--accent")}"></i>p95 per ${per}</span></span>`;
    plot("cache", $("c-cache"), {
      scales: { x: { time: true, range: (u, a, b) => [a - q, b + q] }, y: { range: (u, lo, hi) => [0, (hi || 1) * 1.08] } },
      axes: axes((v) => tok(v), d.bucket),
      series: [{},
        { label: "p95", stroke: css("--accent"), width: 2, fill: css("--accent-soft"), points: { show: true, size: 6, fill: css("--accent") } },
        { label: "median", stroke: css("--cool"), width: 2, dash: [5, 4], points: { show: true, size: 5, fill: css("--cool") } }],
    }, [xs, p95, p50]);
    $("f-cache").innerHTML = d.p95 == null ? "" :
      `<div class="stat" style="color:${css("--cool")}"><span>${span} median</span><b>${tok(d.p50)}</b></div>` +
      `<div class="stat" style="color:${css("--accent")}"><span>${span} p95</span><b>${tok(d.p95)}</b></div>`;
  }

  const secs = (ms) => { ms = +ms || 0; return ms >= 120000 ? (ms / 60000).toFixed(1) + " min" : (ms / 1000).toFixed(ms < 10000 ? 1 : 0) + " s"; };
  const ptr = (r) => (r.pointer ? "transcript " + r.pointer : "");

  function drawBash(d) {
    const rows = d.rows, xs = xsOf(rows), half = stepOf(d) / 2;
    const bar = uPlot.paths.bars({ size: [0.72, 48], align: 0 });
    plot("bash", $("c-bash"), {
      height: 190,
      scales: { x: { time: true, range: (u, a, b) => [a - half, b + half] }, y: { range: (u, a, b) => [0, (b || 1) * 1.08] },
                s: { range: (u, a, b) => [0, (b || 1) * 1.1] } },
      axes: [axes(int, d.bucket)[0], axes(int, d.bucket)[1],
             { scale: "s", side: 1, stroke: css("--muted"), grid: { show: false }, ticks: { show: false },
               font: "11px " + css("--mono"), size: 56, values: (u, vals) => vals.map((v) => secs(v)) }],
      series: [{}, { label: "calls", fill: css("--accent"), stroke: css("--accent"), paths: bar, width: 0, points: { show: false } },
                   { label: "p95", scale: "s", stroke: css("--cool"), width: 2, points: { show: true, size: 5, fill: css("--cool") } }],
    }, [xs, rows.map((r) => r.calls), rows.map((r) => r.p95)]);
  }

  function drawBeforeAfter(d) {
    const rows = d.rows, xs = rows.map((r) => r.t), half = 43200;
    const bar = uPlot.paths.bars({ size: [0.72, 40], align: 0 });
    const mx = d.marker ? dayTs(d.marker.date) : null;
    const ro = $("r-beforeafter");
    const key = `<span class="key"><span><i class="sw" style="background:${css("--accent")}"></i>tool results, MB/day</span>` +
      `<span><i class="sw" style="background:${css("--cool")}"></i>context tokens/day</span>` +
      (d.marker ? `<span><i class="sw" style="background:${css("--ink") || "#888"}"></i>${esc(d.marker.date)} ${esc(d.marker.label)}</span>` : "") + `</span>`;
    ro.innerHTML = key;
    plot("beforeafter", $("c-beforeafter"), {
      scales: { x: { time: true, range: (u, a, b) => [Math.min(a, mx ?? a) - half, Math.max(b, mx ?? b) + half] },
                y: { range: (u, a, b) => [0, (b || 1) * 1.08] }, c: { range: (u, a, b) => [0, (b || 1) * 1.1] } },
      axes: [axes(tok, "day")[0], axes((v) => v + " MB", "day")[1],
             { scale: "c", side: 1, stroke: css("--muted"), grid: { show: false }, ticks: { show: false },
               font: "11px " + css("--mono"), size: 56, values: (u, vals) => vals.map(tok) }],
      series: [{}, { label: "tool MB", fill: css("--accent"), stroke: css("--accent"), paths: bar, width: 0, points: { show: false } },
                   { label: "context", scale: "c", stroke: css("--cool"), width: 2, points: { show: true, size: 5, fill: css("--cool") } }],
      hooks: {
        draw: [noDataHook(rows, half), (u) => { if (mx == null) return; const x = Math.round(u.valToPos(mx, "x", true)); const c = u.ctx;
          if (x < u.bbox.left || x > u.bbox.left + u.bbox.width) return;
          c.save(); c.strokeStyle = css("--ink") || "#888"; c.lineWidth = 2 * devicePixelRatio; c.setLineDash([6 * devicePixelRatio, 4 * devicePixelRatio]);
          c.beginPath(); c.moveTo(x, u.bbox.top); c.lineTo(x, u.bbox.top + u.bbox.height); c.stroke();
          c.setLineDash([]); c.fillStyle = css("--ink") || "#888"; c.font = `${11 * devicePixelRatio}px ${css("--mono")}`;
          c.fillText(d.marker.label, x + 6 * devicePixelRatio, u.bbox.top + 14 * devicePixelRatio); c.restore(); }],
        setCursor: [(u) => { const i = u.cursor.idx; if (i == null) { ro.innerHTML = key; return; }
          const r = rows[i]; if (r.no_data) { ro.textContent = `${r.day}  no data (before the first recorded event)`; return; }
          ro.textContent = `${r.day}  tool results ${r.tool_mb} MB (images ${r.image_mb} MB)  context ${int(r.context)}`; }],
      },
    }, [xs, rows.map((r) => r.tool_mb), rows.map((r) => r.context)]);
    const b = d.before, a = d.after;
    $("f-beforeafter").innerHTML = b && a ? `<b>${b.tool_mb} → ${a.tool_mb} MB/day</b> <span>tool results, 7-day averages before / after ${esc(d.marker.date)}; context ${tok(b.context)} → ${tok(a.context)}/day</span>` : "";
  }

  function render(name, d) {
    $("h-" + name) && ($("h-" + name).textContent = d.hint || "");
    const R = d.rows;
    if (name === "context") drawContext(d);
    if (name === "cache") drawCache(d);
    if (name === "beforeafter") { if (R.length) drawBeforeAfter(d); else $("c-beforeafter").innerHTML = ""; }
    if (name === "tools") {
      $("f-tools").innerHTML = `<b>${bytes(d.total_bytes)}</b> <span>of tool results returned into context` +
        (d.image_bytes ? `; ${pct(d.image_share, 0)} images (${int(d.images)})` : "") + `</span>`;
      drawTools($("t-tools"), R.slice(0, 10), d.total_bytes);
    }
    if (name === "readtypes") drawReadTypes(d);
    if (name === "largest") {
      $("f-largest").innerHTML = R.length ? `<b>${tok(R[0].reread_tokens)}</b> <span>re-read tokens (cumulative): one ${esc(R[0].tool)} output (${bytes(R[0].bytes)}) re-read over ${int(R[0].reread_turns)} turns until the next compaction; session has ${int(R[0].session_requests)} requests, ${int(R[0].session_segments)} segments</span>` : "";
      table($("t-largest"), [
        { label: "tool", key: "tool", cls: "mono" },
        { label: "re-read tokens", num: true, fmt: (r) => tok(r.reread_tokens), title: (r) => `re-read tokens, cumulative: ${int(r.est_tokens)} tokens x ${int(r.reread_turns)} turns until the next compaction` },
        { label: "turns", num: true, fmt: (r) => int(r.reread_turns) },
        { label: "session req", num: true, fmt: (r) => int(r.session_requests) },
        { label: "segments", num: true, fmt: (r) => int(r.session_segments) },
        { label: "bytes", num: true, fmt: (r) => bytes(r.bytes) + (r.images ? ` <span class="tag" title="${esc(bytes(r.image_bytes))} of images">${int(r.images)} img</span>` : ""), title: (r) => int(r.bytes) + " bytes" },
        { label: "sha12", key: "sha12", cls: "mono dim", title: ptr },
        { label: "seen", num: true, fmt: (r) => "x" + r.times_seen },
        { label: "harness", fmt: (r) => htag(r.harness) },
        { label: "session", cls: "mono", fmt: (r) => esc(r.session) + (r.agent === "subagent" ? ' <span class="tag">sub</span>' : "") },
        { label: "project", key: "project" },
      ], R);
    }
    if (name === "repeats") {
      $("f-repeats").innerHTML = `<b>${bytes(d.repeat_bytes)}</b> <span>re-sent across ${int(d.payloads)} payloads</span>`;
      table($("t-repeats"), [
        { label: "sha12", key: "sha12", cls: "mono dim", title: ptr },
        { label: "tool", key: "tool", cls: "mono" },
        { label: "times", num: true, fmt: (r) => "x" + r.times },
        { label: "each", num: true, fmt: (r) => bytes(r.bytes_each) },
        { label: "total", num: true, fmt: (r) => bytes(r.total_bytes), title: (r) => int(r.total_bytes) + " bytes" },
        { label: "re-read tokens", num: true, fmt: (r) => tok(r.reread_tokens), title: (r) => "cumulative, until the next compaction" },
        { label: "turns", num: true, fmt: (r) => int(r.reread_turns) },
        { label: "session req", num: true, fmt: (r) => int(r.session_requests) },
        { label: "segments", num: true, fmt: (r) => int(r.session_segments) },
        { label: "harness", fmt: (r) => esc(r.harness || "") },
        { label: "sessions", num: true, key: "sessions" },
      ], R);
    }
    if (name === "sessions") {
      $("f-sessions").innerHTML = `<b>${int(d.sessions)}</b> <span>sessions, ${int(d.subagent_sessions)} of them subagents</span>`;
      table($("t-sessions"), [
        { label: "session", cls: "mono", fmt: (r) => esc(r.session), title: ptr },
        { label: "harness", fmt: (r) => htag(r.harness) },
        { label: "project", key: "project" },
        { label: "kind", fmt: (r) => r.subagent ? `<span class="tag" title="parent ${esc(r.parent || "")}">subagent</span>` : '<span class="dim">main</span>' },
        { label: "context", num: true, fmt: (r) => tok(r.context), title: (r) => int(r.context) + " tokens" },
        { label: "requests", num: true, fmt: (r) => int(r.requests) },
        { label: "segments", num: true, fmt: (r) => int(r.segments), title: () => "compactions + 1" },
        { label: "days", num: true, fmt: (r) => int(r.days_active) },
        { label: "per req", num: true, fmt: (r) => tok(r.per_request) },
      ], R);
    }
    if (name === "models") {
      const n = R.reduce((a, r) => a + r.requests, 0);
      $("f-models").innerHTML = `<b>${int(n)}</b> <span>model requests across ${R.length} models</span>`;
      bars($("t-models"), R, (r) => r.model, (r) => r.requests, (r) => `${int(r.requests)} <span style="color:var(--muted)">${pct(r.share, 1)}</span>`, true);
      const mx = Math.max(...R.map((r) => r.requests), 1);
      $("t-models").querySelectorAll(".brow").forEach((row, i) => {
        const r = R[i], hs = r.harness_split || [];
        row.querySelector(".track").innerHTML = `<span class="hseg" style="width:${(100 * r.requests / mx).toFixed(1)}%">` +
          hs.map((x) => `<i style="width:${(100 * x.requests / r.requests).toFixed(2)}%;background:${hcolor(x.harness, allH)}" title="${esc(x.harness)}: ${int(x.requests)}"></i>`).join("") + "</span>";
        if (hs.length > 1) row.querySelector(".v").insertAdjacentHTML("beforeend", `<span class="hleg">${hs.map((x) => esc(x.harness) + " " + int(x.requests)).join(" · ")}</span>`);
      });
    }
    if (name === "compactions") {
      const sp = d.split || {}, ss = d.segment_stats || {};
      const days = {}; (d.by_day || []).forEach((x) => { days[x.day] = (days[x.day] || []).concat(x.harness + " " + x.compactions); });
      $("f-compactions").innerHTML = `<b>${int(d.total)}</b> <span>compactions: ${int(sp.manual)} manual, ${int(sp.auto)} auto, ${int(sp.unknown)} unknown; median segment ${int(ss.median_requests)} requests, p95 ${int(ss.p95_requests)}</span>` +
        `<div class="dim">${Object.keys(days).map((k) => esc(k) + ": " + esc(days[k].join(", "))).join(" · ")}</div>`;
      bars($("t-compactions-split"), [["manual", sp.manual], ["auto", sp.auto], ["unknown", sp.unknown]], (r) => r[0], (r) => r[1] || 0, (r) => int(r[1]));
      bars($("t-compactions-sizes"), d.sizes || [], (r) => r.bucket + " requests", (r) => r.segments, (r) => int(r.segments) + " segments");
      table($("t-compactions"), [
        { label: "session", key: "session", cls: "mono" },
        { label: "compactions", num: true, fmt: (r) => int(r.compactions), title: (r) => `${int(r.manual)} manual, ${int(r.auto)} auto, ${int(r.unknown)} unknown` },
        { label: "requests", num: true, fmt: (r) => int(r.requests) },
        { label: "avg segment req", num: true, fmt: (r) => int(r.avg_segment_requests) },
        { label: "p95 segment req", num: true, fmt: (r) => int(r.p95_segment_requests) },
        { label: "avg segment ctx", num: true, fmt: (r) => tok(r.avg_segment_context) },
        { label: "p95 segment ctx", num: true, fmt: (r) => tok(r.p95_segment_context) },
      ], R);
    }
    if (name === "loops") {
      $("f-loops").innerHTML = `<b>${int(d.loops)}</b> <span>loops detected</span>`;
      table($("t-loops"), [
        { label: "session", key: "session", cls: "mono", title: ptr },
        { label: "harness", fmt: (r) => htag(r.harness) },
        { label: "tool", key: "tool", cls: "mono" },
        { label: "calls", num: true, fmt: (r) => "x" + r.attempts },
        { label: "kind", fmt: (r) => r.kind === "tool error" ? '<span class="tag err">tool error</span>' : '<span class="dim">identical, no error</span>' },
        { label: "est. tokens", num: true, fmt: (r) => tok(r.est_tokens) },
        { label: "started", key: "seen_at", cls: "mono dim" },
      ], R);
    }
    if (name === "subagents") {
      $("f-subagents").innerHTML = d.spawns ? `<b>${int(d.spawns)}</b> <span>spawn${d.spawns === 1 ? "" : "s"}; ${pct(d.default / d.spawns, 0)} on the default model, ` +
        `${pct(d.explicit / d.spawns, 0)} named one</span>` : "";
      bars($("t-subagents"), R, (r) => r.subagent_type, (r) => r.context,
        (r) => `${tok(r.context)} <span style="color:var(--muted)">x${int(r.spawns)}</span>`, true);
      const lanes = (d.lanes_most || []).map((l) => Object.assign({ side: "most" }, l))
        .concat((d.lanes_least || []).filter((l) => !(d.lanes_most || []).some((m) => m.lane === l.lane)).map((l) => Object.assign({ side: "least" }, l)));
      const req = (d.requested || []).map((r) => `${esc(r.model)} ${int(r.spawns)}`).join(" · ");
      $("l-subagents").innerHTML = "";
      if (lanes.length) table($("l-subagents"), [
        { label: "lane", key: "lane", cls: "mono" },
        { label: "delegates", fmt: (r) => r.side === "most" ? '<span class="tag">most</span>' : '<span class="dim">least</span>' },
        { label: "spawns", num: true, fmt: (r) => int(r.spawns) },
        { label: "tool calls", num: true, fmt: (r) => int(r.calls) },
        { label: "per 100", num: true, fmt: (r) => (+r.per_100).toFixed(2) },
      ], lanes);
      if (req) $("l-subagents").insertAdjacentHTML("afterbegin", `<p class="readout">model requested: ${req}</p>`);
    }
    if (name === "skills") {
      $("f-skills").innerHTML = d.calls ? `<b>${int(d.calls)}</b> <span>invocations of ${int(d.skills)} skills</span>` : "";
      bars($("t-skills"), R.slice(0, 10), (r) => r.skill, (r) => r.calls,
        (r) => `${int(r.calls)} <span style="color:var(--muted)">${esc(r.kinds)}</span>`);
      $("k-skills").textContent = (d.kinds || []).map((k) => `${k.kind} ${int(k.calls)} (${int(k.skills)} skills)`).join(" · ");
    }
    if (name === "bash") {
      const T = d.shell_total || {};
      $("f-bash").innerHTML = T.calls ? `<b>${tok(T.tokens)}</b> <span>tokens returned by ${int(T.calls)} shell calls${T.tail ? "; re-read tokens " + tok(T.tail) : ""}` +
        (d.calls ? `; p95 ${secs(d.p95)}, median ${secs(d.p50)}` : "") + "</span>" : "";
      drawShell($("d-bash"), d.families || []);
      if (R.length) drawBash(d); else $("c-bash").innerHTML = "";
      table($("t-bash"), [
        { label: "slowest 10 (masked)", cls: "mono", fmt: (r) => esc(r.command), title: ptr },
        { label: "took", num: true, fmt: (r) => secs(r.duration_ms) },
        { label: "exit", fmt: (r) => r.is_error ? `<span class="tag err">${r.exit_code != null ? "exit " + r.exit_code : "error"}</span>` : '<span class="dim">ok</span>' },
        { label: "harness", fmt: (r) => htag(r.harness) },
        { label: "session", key: "session", cls: "mono" },
        { label: "at", key: "seen_at", cls: "mono dim" },
      ], d.slowest || []);
    }
  }

  // Panel 03: tool -> (family -> target | file kind -> ext -> top files), sorted by bytes, re-read tokens alongside.
  function drawTools(el, R, total) {
    if (!R.length) { el.innerHTML = '<p class="empty">Nothing recorded for this range.</p>'; return; }
    const node = (r) => ({ label: r.tool, bytes: r.bytes, tail: r.reread_tokens, n: r.results,
      extra: r.image_bytes ? "img " + pct(r.image_share, 0) : "",
      kids: (r.families || []).map((f) => ({ label: f.family, bytes: f.bytes, tail: f.tail, n: f.calls,
               kids: f.targets.map((t) => ({ label: t.target, bytes: t.bytes, tail: t.tail, n: t.calls })) }))
        .concat((r.kinds || []).map((k) => ({ label: k.kind, bytes: k.bytes, tail: k.tail, n: k.calls,
               kids: k.exts.map((x) => ({ label: "." + x.ext, bytes: x.bytes, tail: x.tail, n: x.calls,
                 kids: x.files.map((f) => ({ label: f.path, bytes: f.bytes, tail: f.tail, n: f.calls, leaf: true })) })) }))) });
    let h = '<table class="shell tools"><thead><tr><th>tool / family / target</th><th class="num">bytes</th><th class="num">share</th><th class="num">re-read tokens</th><th class="num">results</th></tr></thead><tbody>', id = 0;
    const walk = (n, depth, parent) => {
      const k = "k" + (id++), has = n.kids && n.kids.length;
      h += `<tr class="${depth ? "sh-t" : "sh-f"}" ${has ? `data-k="${k}"` : ""} ${parent ? `data-p="${parent}" hidden` : ""}>` +
        `<td class="mono${n.leaf ? " dim" : ""}" style="padding-left:${depth * 18 + 4}px" title="${esc(n.label)}">${has ? '<span class="car">&#9656;</span> ' : ""}${depth ? esc(n.label) : "<b>" + esc(n.label) + "</b>"}</td>` +
        `<td class="num">${bytes(n.bytes)}</td><td class="num dim">${pct(n.bytes / (total || 1), 1)}</td><td class="num">${n.tail ? tok(n.tail) : ""}</td><td class="num">${int(n.n)}${n.extra ? ' <span class="dim">' + esc(n.extra) + "</span>" : ""}</td></tr>`;
      (n.kids || []).forEach((c) => walk(c, depth + 1, k));
    };
    R.forEach((r) => walk(node(r), 0, null));
    el.innerHTML = h + "</tbody></table>";
    el.onclick = (e) => {
      const tr = e.target.closest("tr[data-k]"); if (!tr) return;
      const open = !tr.classList.toggle("open"), k = tr.dataset.k;
      const kids = (key) => [...el.querySelectorAll(`tr[data-p="${key}"]`)];
      const hide = (key) => kids(key).forEach((c) => { c.hidden = true; c.classList.remove("open"); hide(c.dataset.k || ""); });
      kids(k).forEach((c) => { c.hidden = open; if (open) { c.classList.remove("open"); hide(c.dataset.k || ""); } });
    };
  }

  const KIND_FILL = ["#c8553d", "#2f6f8f", "#6b8e23", "#b8860b", "#8e5ea2", "#3c9d8f", "#a0522d", "#708090", "#999"];

  // Panel 12: stacked bytes per file kind per bucket, plus top paths by re-read tokens.
  function drawReadTypes(d) {
    const rows = d.rows, kinds = (d.kinds || []).map((k) => k.kind);
    $("f-readtypes").innerHTML = d.total_bytes ? `<b>${bytes(d.total_bytes)}</b> <span>returned by file reads; ${esc((d.kinds[0] || {}).kind)} ${pct((d.kinds[0] || {}).share || 0, 0)}</span>` : "";
    if (!rows.length) { $("c-readtypes").innerHTML = ""; $("r-readtypes").innerHTML = ""; $("t-readtypes").innerHTML = '<p class="empty">Nothing recorded for this range.</p>'; return; }
    const xs = xsOf(rows), half = stepOf(d) / 2, bar = uPlot.paths.bars({ size: [0.72, 48], align: 0 });
    const col = (i) => KIND_FILL[i % KIND_FILL.length];
    const cum = rows.map(() => 0), data = [xs], series = [{}];
    const stacks = kinds.map((k) => rows.map((r, j) => (cum[j] += r[k] || 0)));
    // draw the tallest cumulative first so lower kinds sit on top of it
    for (let i = kinds.length - 1; i >= 0; i--) {
      data.push(stacks[i]);
      series.push({ label: kinds[i], fill: col(i), stroke: col(i), paths: bar, width: 0, points: { show: false } });
    }
    $("r-readtypes").innerHTML = '<span class="key">' + kinds.map((k, i) => `<span><i class="sw" style="background:${col(i)}"></i>${esc(k)}</span>`).join("") + "</span>";
    plot("readtypes", $("c-readtypes"), {
      height: 220,
      scales: { x: { time: true, range: (u, a, b) => [a - half, b + half] }, y: { range: (u, a, b) => [0, (b || 1) * 1.08] } },
      axes: axes(bytes, d.bucket), series,
    }, data);
    table($("t-readtypes"), [
      { label: "file (top 15 by re-read tokens)", cls: "mono", fmt: (r) => esc(r.path), title: (r) => r.path },
      { label: "kind", fmt: (r) => esc(r.kind) },
      { label: "reads", num: true, fmt: (r) => int(r.calls) },
      { label: "bytes", num: true, fmt: (r) => bytes(r.bytes) },
      { label: "re-read tokens", num: true, fmt: (r) => tok(r.tail) },
      { label: "sessions", num: true, fmt: (r) => int(r.sessions) },
      { label: "lane", key: "lane", cls: "mono dim" },
    ], d.files || []);
  }

  // Panel 11: family -> target -> calls, ranked by tokens returned; duration is a secondary column.
  function drawShell(el, fams) {
    if (!fams.length) { el.innerHTML = '<p class="empty">No shell results recorded for this range.</p>'; return; }
    const max = Math.max(...fams.map((f) => f.tokens), 1);
    const head = (a, b) => `<tr><th>${a}</th><th class="num">tokens returned</th><th class="num">re-read tokens</th><th class="num">calls</th><th class="num">tokens/call</th><th class="num">time</th><th>${b}</th></tr>`;
    let h = `<table class="shell"><thead>${head("family / target / call", "")}</thead><tbody>`;
    fams.forEach((f, i) => {
      h += `<tr class="sh-f" data-k="f${i}"><td class="mono"><span class="car">&#9656;</span> <b>${esc(f.family)}</b></td>` +
        `<td class="num">${tok(f.tokens)}</td><td class="num">${f.tail ? tok(f.tail) : ""}</td>` +
        `<td class="num">${int(f.calls)}</td><td class="num">${tok(Math.round(f.calls ? f.tokens / f.calls : 0))}</td><td class="num dim">${secs(f.duration_ms)}</td><td></td></tr>`;
      f.targets.forEach((t, j) => {
        h += `<tr class="sh-t" data-p="f${i}" data-k="f${i}t${j}" hidden><td class="mono" style="padding-left:22px"><span class="car">&#9656;</span> ${esc(t.target)}</td>` +
          `<td class="num">${tok(t.tokens)}</td><td class="num">${t.tail ? tok(t.tail) : ""}</td><td class="num">${int(t.calls)}</td>` +
          `<td class="num">${tok(Math.round(t.calls ? t.tokens / t.calls : 0))}</td><td class="num dim">${secs(t.duration_ms)}</td><td></td></tr>`;
        (t.top || []).forEach((c) => {
          h += `<tr class="sh-c" data-p="f${i}t${j}" hidden title="${esc(ptr(c))}"><td class="mono dim" style="padding-left:44px">${esc(c.command || "(not recorded)")}</td>` +
            `<td class="num">${tok(c.tokens)}</td><td class="num"></td><td class="num dim">${int(c.bytes)} B</td><td class="num"></td>` +
            `<td class="num dim">${c.duration_ms != null ? secs(c.duration_ms) : ""}</td><td class="mono dim">${esc(c.harness || "")} ${esc(c.session)} ${esc(c.pointer || "")}</td></tr>`;
        });
      });
      if (f.more_targets) h += `<tr class="sh-t" data-p="f${i}" hidden><td class="dim" style="padding-left:22px" colspan="7">+${f.more_targets} more targets</td></tr>`;
    });
    el.innerHTML = h + "</tbody></table>";
    el.onclick = (e) => {
      const tr = e.target.closest("tr[data-k]"); if (!tr) return;
      const open = !tr.classList.toggle("open"), k = tr.dataset.k;
      const kids = (key) => [...el.querySelectorAll(`tr[data-p="${key}"]`)];
      kids(k).forEach((c) => { c.hidden = open; if (open) { c.classList.remove("open"); kids(c.dataset.k || "").forEach((g) => (g.hidden = true)); } });
    };
  }

  function renderMeta(d) {
    const m = d.rows[0];
    if (STATIC) {
      $("status").textContent = `${m.day} UTC · ${int(m.events)} events · generated ${m.now.replace("T", " ").slice(0, 16)} UTC` + (m.coverage && m.coverage.earliest_event ? ` · data on this host starts ${m.coverage.earliest_event.slice(0, 10)}` : "");
      $("rec").className = "rec";
    }
    const w = m.watch_status;
    const rec = $("rec");
    const cv = m.coverage, since = cv && cv.earliest_event ? cv.earliest_event.slice(0, 10) : null;
    const a = m.range_start || m.first_day, b = m.range_end || m.last_day;
    $("range").textContent = a && b ? (a === b ? a : `${a} → ${b}`) : "";
    let s = m.last_event ? `last event ${ago(m.last_event)}` : "nothing recorded yet";
    if (STATIC) { /* status already set */ } else if (w) {
      rec.className = "rec " + (w.ok && Date.now() - Date.parse(w.last_run) < 3 * (w.interval_min || 10) * 60000 ? "live" : "stale");
    } else rec.className = "rec";
    if (!STATIC) $("status").textContent = s;
    const k = [
      [tok(m.context_tokens), "total tokens", m.cache_hit_rate != null ? [pct(m.cache_hit_rate, 0), "cache hit rate"] : null],
      [tok(m.output_tokens), "output tokens"],
      [int(m.requests), "model requests"],
      [bytes(m.tool_bytes), m.image_bytes ? `tool result bytes (${pct(m.image_bytes / (m.tool_bytes || 1), 0)} images)` : "tool result bytes"],
      [int(m.sessions), "sessions", [int(m.subagent_sessions), m.subagent_sessions === 1 ? "subagent" : "subagents"]],
      [m.cost_usd ? "$" + Math.round(m.cost_usd).toLocaleString("en-US") : "n/a", "API cost (where reported)"],
      [m.list_cost_usd ? "$" + Math.round(m.list_cost_usd).toLocaleString("en-US") : "n/a", `API list price (${int(m.list_unpriced)} requests unpriced)`],
    ];
    if (m.prices_as_of) $("prices").textContent = `${m.prices_as_of}. Reported cost $${(+m.cost_usd || 0).toFixed(2)} (harness, where recorded); list-price cost $${(+m.list_cost_usd || 0).toFixed(2)}.`;
    $("kpis").innerHTML = k.map(([v, l, x]) => `<div class="kpi"><div class="v">${esc(v)}</div><div class="k">${esc(l)}</div>` +
      (x ? `<div class="x"><b>${esc(x[0])}</b> ${esc(x[1])}</div>` : "") + `</div>`).join("");
  }

  const PANELS = ["context", "cache", "tools", "largest", "repeats", "sessions", "models", "loops", "subagents", "skills", "bash", "readtypes", "compactions", "beforeafter"];

  function drawChips(hs) {
    allH = hs.map((x) => x.harness);
    const el = $("chips");
    el.innerHTML = hs.length < 2 && !hsel.length ? "" : hs.map((x) => `<button data-h="${esc(x.harness)}" aria-pressed="${hsel.includes(x.harness)}" ` +
      `title="${int(x.requests)} model requests (all time in store)">${esc(x.harness)}<small>${int(x.requests)}</small></button>`).join("");
    el.onclick = (e) => {
      const b = e.target.closest("button[data-h]"); if (!b) return;
      const h = b.dataset.h;
      hsel = hsel.includes(h) ? hsel.filter((x) => x !== h) : hsel.concat(h);
      setHash(); load();
    };
  }

  async function load() {
    document.querySelectorAll(".seg button").forEach((b) => b.setAttribute("aria-pressed", String(+b.dataset.days === days)));
    try { const m = await get("meta"); if (!STATIC) drawChips(m.rows[0].harnesses || []); renderMeta(m); }
    catch (e) { $("status").textContent = "Recorder unavailable: " + e.message; return; }
    if (STATIC) document.querySelector(".seg") && (document.querySelector(".seg").style.display = "none");
    await Promise.all(PANELS.map(async (p) => {
      try { cache[p] = await get(p); render(p, cache[p]); }
      catch (e) { const h = $("h-" + p); if (h) h.textContent = "Could not read this panel: " + e.message; }
    }));
  }

  document.querySelectorAll(".seg button").forEach((b) => b.addEventListener("click", () => {
    days = +b.dataset.days; setHash(); load();
  }));
  let t;
  const redraw = () => { clearTimeout(t); t = setTimeout(() => { cache.context && drawContext(cache.context); cache.cache && drawCache(cache.cache);
    cache.bash && cache.bash.rows.length && drawBash(cache.bash);
    cache.readtypes && cache.readtypes.rows.length && drawReadTypes(cache.readtypes);
    cache.beforeafter && cache.beforeafter.rows.length && drawBeforeAfter(cache.beforeafter); }, 120); };
  window.addEventListener("resize", redraw);
  matchMedia("(prefers-color-scheme: dark)").addEventListener("change", redraw);
  load();
  if (!STATIC) setInterval(load, 5 * 60 * 1000);
})();

// The logo sizes itself to the header block beside it (wide layout) or stays small (narrow layout).
(function () {
  const box = document.querySelector(".logobox"), head = document.querySelector(".head");
  if (!box || !head || typeof ResizeObserver === "undefined") return;
  const fit = () => {
    const s = window.innerWidth <= 720 ? 64 : Math.max(64, Math.min(128, head.offsetHeight));
    box.style.width = box.style.height = s + "px";
  };
  new ResizeObserver(fit).observe(head); window.addEventListener("resize", fit); fit();
})();
