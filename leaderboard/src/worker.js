// GPU Transcode Benchmark leaderboard — Cloudflare Worker + D1.
// Implements docs/superpowers/specs/2026-07-07-leaderboard-submission-contract.md exactly:
//   POST /api/submit         — validated envelope ingest (upsert best per install+gpu+profile)
//   GET  /api/top            — median-per-GPU board rows (?profile=..., canonical by default)
//   GET  /api/detail|profiles — public read-only board data
//   POST /api/admin/hide|restore?id= — moderation (Authorization: Bearer <ADMIN_TOKEN>, audited)
//   GET  /                   — the public leaderboard page
const SCHEMA = 1;
const ACCEPTED_MAJOR = "1";                       // result.tool_version major versions accepted
const CANONICAL = "4K HEVC -> 1080p H264";
const RATE_PER_HOUR = 30;                         // submissions per ip_hash per hour
const MAX_BODY = 32 * 1024;

// CORS is PER-ROUTE: public read-only GETs may be embedded anywhere (jsonPub); the submit and
// admin POSTs get NO CORS headers and NO preflight approval — an arbitrary webpage cannot use
// visitors' browsers to post submissions. (Scripted/curl submissions are unaffected; this
// closes the cheap browser-distributed-abuse route only. The strict application/json content
// type stays REQUIRED — text/plain or form posts can skip preflight entirely.)
const json = (obj, status = 200, extra = {}) =>
  new Response(JSON.stringify(obj), { status, headers: { "Content-Type": "application/json",
    "Cache-Control": "no-store", ...extra } });
const jsonPub = (obj, status = 200) => json(obj, status, { "Access-Control-Allow-Origin": "*" });
const bad = (error, status = 400, extra = {}) => json({ ok: false, error }, status, extra);

async function sha256hex(s) {
  const d = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(s));
  return [...new Uint8Array(d)].map(b => b.toString(16).padStart(2, "0")).join("");
}

// ---- validation (the contract's server checklist) --------------------------------------------
function num(v) { return typeof v === "number" && isFinite(v); }

const VENDORS = ["intel", "amd", "nvidia"];
const IN_CODECS = ["h264", "hevc", "av1", "hdr"];   // "hdr" = the HDR10 tone-map profile
const OUT_CODECS = ["h264", "hevc", "av1"];

// The profile string is DERIVED from the validated structured fields and must match what the
// client sent — a submission cannot invent arbitrary profile boards (mirrors the container's
// profile_label(); comparable runs are always 4K→1080p non-custom, so no custom/res variants)
function expectedProfile(r) {
  return "4K " + String(r.input_codec).toUpperCase() + " -> 1080p "
    + String(r.codec).toUpperCase() + (r.subs_burn ? " + subs" : "");
}

function capStr(v, max) { return typeof v === "string" && v.length <= max; }

function validate(env0) {
  if (!env0 || typeof env0 !== "object") return "bad envelope";
  if (env0.schema !== SCHEMA) return "unknown schema";
  const iid = env0.install_id;
  if (typeof iid !== "string" || iid.length < 8 || iid.length > 64) return "bad install_id";
  const r = env0.result;
  if (!r || typeof r !== "object") return "missing result";
  // re-derive comparability — never trust the flag alone
  const comparable = r.mode === "streaming" && r.source_res === "4k" && r.target_res === "1080p"
    && !r.custom_source && !r.is_cpu && r.comparable === true;
  if (!comparable) return "not a comparable run";
  const major = String(r.tool_version || "").split(".")[0];
  if (major !== ACCEPTED_MAJOR) return "unsupported tool version";
  // a lowered PASS_THRESHOLD inflates stream counts — only strict-realtime runs are comparable
  if (r.threshold !== 1) return "non-standard pass threshold";
  // a short hold submits BURST performance as "sustained" and passes every other check —
  // the timing exploit that would otherwise own any headline statistic
  if (!num(r.hold_seconds) || r.hold_seconds < 25) return "non-standard hold duration";
  if (!num(r.settle_seconds) || r.settle_seconds < 5) return "non-standard settle duration";
  // the pinned-bitstream guarantee: a locally generated (hash-mismatched) clip is not the
  // canonical workload. NOTE: this also rejects pre-clips-round client builds (field absent) —
  // deliberate, same policy as the threshold field before it.
  if (r.clip_verified !== true) return "clip not verified against the pinned release";
  if (!VENDORS.includes(r.vendor)) return "unknown vendor";
  if (!IN_CODECS.includes(r.input_codec)) return "unknown input codec";
  if (!OUT_CODECS.includes(r.codec)) return "unknown output codec";
  if (r.ten_bit === true) return "10-bit output is not a comparable profile";  // 4K→1080p is 8-bit
  if (typeof r.gpu !== "string" || !r.gpu.length || r.gpu.length > 120) return "bad gpu";
  // profile must equal the string DERIVED from the validated fields — no invented boards
  if (r.profile !== expectedProfile(r)) return "profile does not match run parameters";
  // length caps on every stored display string (defence in depth alongside output escaping)
  for (const [k, max] of [["driver", 60], ["os_version", 60], ["kernel", 60],
                          ["ram", 40], ["cpu", 120]])
    if (r[k] != null && !capStr(r[k], max)) return "bad " + k;
  if (!Number.isInteger(r.max_sustained) || r.max_sustained < 1 || r.max_sustained > 128)
    return "max_sustained out of range";
  if (!num(r.single_stream) || r.single_stream <= 0 || r.single_stream > 100)
    return "single_stream out of range";
  if (!num(r.peak_combined) || r.peak_combined <= 0 || r.peak_combined > 128)
    return "peak_combined out of range";
  for (const k of ["watts_per_stream", "peak_power_w", "load_power_w", "idle_power_w"])
    if (r[k] != null && (!num(r[k]) || r[k] < 0 || r[k] > 2000)) return k + " out of range";
  // internal consistency: per_level must SUPPORT the headline (forging a coherent curve is work)
  const pl = r.per_level;
  if (!Array.isArray(pl) || !pl.length || pl.length > 200) return "bad per_level";
  // "passing" must mirror the client (worst >= 1.0) — but per_level stores worst ROUNDED to
  // 3 decimals, so a stored 1.000 is AMBIGUOUS: the raw value was anywhere in [0.9995, 1.0005),
  // i.e. either a marginal client FAIL (0.9997 → max stays one lower) or a marginal pass. A
  // strict equality check here rejected an honest RX 9070 XT run that failed level 8 at a raw
  // ~0.9997 (stored as 1.0). So: definite-pass (>= 1.0005) sets the FLOOR for max_sustained,
  // possible-pass (>= 0.9995, ambiguous included) sets the CEILING. (A forger gains at most the
  // one knife-edge level from the ambiguity — negligible vs rejecting real marginal runs.)
  // Historical: a looser 0.95 tolerance likewise rejected honest 0.95–0.999 marginal fails.
  let highestDefinite = 0, highestPossible = 0, maxCombined = 0;
  for (let i = 0; i < pl.length; i++) {
    const L = pl[i];
    if (!L || !Number.isInteger(L.n) || !num(L.worst) || !num(L.combined)) return "bad per_level row";
    // the ramp is strictly sequential from 1 — reject shuffled/duplicated/gapped curves
    if (L.n !== i + 1) return "per_level must be sequential from 1";
    if (L.worst < 0 || L.worst > 100 || L.combined < 0 || L.combined > 256) return "per_level out of range";
    if (L.worst >= 1.0005) highestDefinite = Math.max(highestDefinite, L.n);
    if (L.worst >= 0.9995) {
      highestPossible = Math.max(highestPossible, L.n);
      maxCombined = Math.max(maxCombined, L.combined);
    }
  }
  if (r.max_sustained < highestDefinite || r.max_sustained > highestPossible)
    return "per_level does not support max_sustained";
  if (Math.abs(maxCombined - r.peak_combined) > 0.1 * Math.max(maxCombined, r.peak_combined))
    return "peak_combined inconsistent with per_level";
  return null;
}

// ---- routes -----------------------------------------------------------------------------------
async function handleSubmit(request, env) {
  // emergency kill switch: set SUBMISSIONS_ENABLED=false (Worker var) to pause ingest during
  // an abuse wave without touching moderation or the read routes
  if (env.SUBMISSIONS_ENABLED === "false")
    return bad("submissions are temporarily paused", 503);
  // fail CLOSED when the salt secret is missing — never fall back to a publicly known value
  if (!env.RATE_SALT) {
    console.error("RATE_SALT is not configured");
    return bad("service unavailable", 503);
  }
  if ((request.headers.get("Content-Type") || "").indexOf("application/json") < 0)
    return bad("expected application/json", 415);
  // reject a declared-oversized body BEFORE reading it (the post-read check stays: the
  // Content-Length header may be absent or wrong)
  const declared = parseInt(request.headers.get("Content-Length") || "0", 10);
  if (declared > MAX_BODY) return bad("too large", 413);
  const body = await request.text();
  if (new TextEncoder().encode(body).length > MAX_BODY) return bad("too large", 413);
  let envelope;
  try { envelope = JSON.parse(body); } catch { return bad("invalid json"); }
  const err = validate(envelope);
  if (err) return bad(err);

  // rate limit per hashed ip (raw IP never stored) — salt lives in a Worker secret
  const ip = request.headers.get("CF-Connecting-IP") || "0.0.0.0";
  const ipHash = (await sha256hex(env.RATE_SALT + ip)).slice(0, 32);
  const hourAgo = Math.floor(Date.now() / 1000) - 3600;
  const { results: rl } = await env.DB.prepare(
    "SELECT COUNT(*) AS c FROM ratelimit WHERE ip_hash = ? AND ts > ?").bind(ipHash, hourAgo).all();
  if (rl[0].c >= RATE_PER_HOUR)
    return bad("rate limited", 429, { "Retry-After": "3600" });
  const now = Math.floor(Date.now() / 1000);
  await env.DB.prepare("INSERT INTO ratelimit (ip_hash, ts) VALUES (?, ?)").bind(ipHash, now).run();
  // probabilistic cleanup: expired ip-hash rows are useless — don't retain them indefinitely
  if (Math.random() < 0.05)
    await env.DB.prepare("DELETE FROM ratelimit WHERE ts < ?").bind(hourAgo).run();

  const r = envelope.result;
  // upsert: keep the BEST run per (install, gpu, profile); resubmits update, never stack
  await env.DB.prepare(`
    INSERT INTO submissions (install_id, gpu, vendor, profile, tool_version, max_sustained,
      capped, projected, single_stream, peak_combined, watts_per_stream, power_estimated,
      driver, os_version, kernel, ram, cpu, submitted_at, updated_at, ip_hash, raw)
    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    ON CONFLICT(install_id, gpu, profile) DO UPDATE SET
      max_sustained=excluded.max_sustained, capped=excluded.capped, projected=excluded.projected,
      single_stream=excluded.single_stream, peak_combined=excluded.peak_combined,
      watts_per_stream=excluded.watts_per_stream, power_estimated=excluded.power_estimated,
      tool_version=excluded.tool_version, driver=excluded.driver, os_version=excluded.os_version,
      kernel=excluded.kernel, ram=excluded.ram, cpu=excluded.cpu,
      updated_at=excluded.updated_at, ip_hash=excluded.ip_hash, raw=excluded.raw
    WHERE excluded.max_sustained >= submissions.max_sustained`).bind(
      envelope.install_id, r.gpu, r.vendor || null, r.profile, r.tool_version,
      r.max_sustained, r.capped ? 1 : 0, r.projected ?? null, r.single_stream,
      r.peak_combined, r.watts_per_stream ?? null, r.power_estimated ? 1 : 0,
      r.driver || null, r.os_version || null, r.kernel || null, r.ram || null, r.cpu || null,
      // server receipt time is authoritative (the client's submitted_at stays in raw only)
      now, now, ipHash, body).run();
  return json({ ok: true });
}

const median = a => {
  const s = [...a].sort((x, y) => x - y);
  return s.length ? (s.length % 2 ? s[(s.length - 1) / 2]
                                  : (s[s.length / 2 - 1] + s[s.length / 2]) / 2) : null;
};

async function handleTop(url, env) {
  const profile = url.searchParams.get("profile") || CANONICAL;
  // integrity/clean fields live in the raw envelope — read via json_extract so the CLEAN
  // definition can evolve at query time with no schema migration
  const { results } = await env.DB.prepare(
    `SELECT gpu, vendor, max_sustained, capped, projected, watts_per_stream, ram,
            CAST(json_extract(raw,'$.result.busy_load') AS REAL) AS busy_load,
            CAST(json_extract(raw,'$.result.vram_total_mb') AS REAL) AS vt,
            CAST(json_extract(raw,'$.result.vram_free_start_mb') AS REAL) AS vf,
            json_extract(raw,'$.result.is_igpu') AS is_igpu,
            json_extract(raw,'$.result.limit_reason') AS limit_reason
     FROM submissions WHERE profile = ? AND hidden = 0`).bind(profile).all();

  // CLEAN = low pre-run engine load AND (when VRAM fields exist) low baseline VRAM.
  // Free VRAM alone is not an idle card; missing fields (pre-integrity rows) = not clean.
  const isClean = r => r.busy_load != null && r.busy_load < 15
    && (r.vt == null || r.vf == null || (r.vt - r.vf) <= Math.max(2048, r.vt * 0.10));
  const ramGen = ram => { const u = String(ram || "").toUpperCase();
    return u.startsWith("DDR5")||u.startsWith("LPDDR5") ? "DDR5"
         : u.startsWith("DDR4")||u.startsWith("LPDDR4") ? "DDR4"
         : u.startsWith("DDR3") ? "DDR3" : null; };

  // ENTITIES: iGPU RAM generation is intrinsic capability → separate entities ("UHD 770
  // (DDR5)"), like 3060 8GB vs 12GB. Cap-state remains the ONLY row-split for a given
  // entity. Cleanliness is a FILTER over runs, NEVER a row-split.
  const byKey = new Map();
  for (const row of results) {
    const igpu = row.is_igpu === 1 || row.is_igpu === true || row.ram != null;
    const gen = igpu ? (ramGen(row.ram) || "RAM unknown") : null;
    const entity = gen ? row.gpu + " (" + gen + ")" : row.gpu;
    // the config split is SESSION-capped vs everything else: a memory/unknown-walled run is
    // the silicon genuinely delivering (grouping it with session-capped runs re-creates the
    // median-15 blend). Legacy rows (no limit_reason) with capped=true WERE session caps.
    const sess = row.capped && (row.limit_reason == null || row.limit_reason === "session");
    const fullEntity = entity + (sess ? " (driver session cap)" : "");
    const k = fullEntity;
    if (!byKey.has(k)) byKey.set(k, { entity: fullEntity, base_gpu: row.gpu, ram_gen: gen,
      vendor: row.vendor, capped: sess, rows: [] });
    byKey.get(k).rows.push({ ...row, clean: isClean(row) });
  }

  const stats = rows => rows.length ? {
    median: median(rows.map(r => r.max_sustained)),
    best: Math.max(...rows.map(r => r.max_sustained)),
    min: Math.min(...rows.map(r => r.max_sustained)),
    count: rows.length,
    median_wps: (a => a.length ? Math.round(median(a) * 100) / 100 : null)
      (rows.filter(r => r.watts_per_stream != null).map(r => r.watts_per_stream)),
    median_projected: (a => a.length ? median(a) : null)
      (rows.filter(r => r.capped && r.projected != null).map(r => r.projected)),
  } : null;

  const out = [...byKey.values()].map(g => {
    const clean = stats(g.rows.filter(r => r.clean));
    const all = stats(g.rows);
    const shown = clean || all;                 // no clean runs ⇒ mark-and-show the loaded stats
    return {
      gpu: g.entity, base_gpu: g.base_gpu, ram_gen: g.ram_gen, vendor: g.vendor,
      session_capped: g.capped,
      mostly_capped: g.capped,
      median_streams: shown.median, best_streams: all.best, min_streams: shown.min,
      median_wps: shown.median_wps, median_projected: shown.median_projected,
      count: all.count, clean_count: clean ? clean.count : 0,
      all_median: all.median, all_count: all.count,
      provisional: !!clean && clean.count === 1,   // a median of one is that run in disguise
      understated: !clean,                          // measured under load — may understate
    };
  // ranking = clean median (or the marked loaded fallback); IDENTICAL in both toggle states
  }).sort((a, b) => b.median_streams - a.median_streams);
  return jsonPub({ profile, rows: out });
}

async function handleDetail(url, env) {
  // per-GPU drill-down, AGGREGATED so it is exact at any scale (no latest-N sampling):
  //  dist   — the full joint (ram, streams) distribution via GROUP BY: a handful of numbers
  //           whether there are 6 submissions or 60,000; the page derives the histogram,
  //           exact medians and the RAM punchline from it
  //  top    — the 3 best setups with their full recipe (the buyer's shopping list)
  //  recent — latest 10 (activity/honesty feed)
  // All fields are ALLOWLISTED — never install_id, ip_hash or the raw envelope.
  const gpu = url.searchParams.get("gpu");
  if (!gpu) return bad("missing gpu");
  const profile = url.searchParams.get("profile") || CANONICAL;
  const gen = url.searchParams.get("gen");         // iGPU entity filter (DDR4/DDR5/unknown)
  const cap = url.searchParams.get("cap");         // entity cap-config filter (1=session-capped)
  let where = "profile = ? AND gpu = ? AND hidden = 0";
  if (gen === "RAM unknown") where += " AND ram IS NULL";
  else if (gen) where += " AND ram LIKE '" + gen.replace(/[^A-Z0-9]/gi, "") + "%'";
  const sessSql = "(capped = 1 AND COALESCE(json_extract(raw,'$.result.limit_reason'),'session') = 'session')";
  if (cap === "1") where += " AND " + sessSql;
  else if (cap === "0") where += " AND NOT " + sessSql;
  const { results: dist } = await env.DB.prepare(
    `SELECT ram, max_sustained AS streams, COUNT(*) AS count
     FROM submissions WHERE ${where} GROUP BY ram, max_sustained`).bind(profile, gpu).all();
  const fields = `max_sustained, capped, projected, watts_per_stream, ram, cpu, driver,
                  os_version, updated_at,
                  json_extract(raw,'$.result.limit_reason') AS limit_reason`;
  const { results: top } = await env.DB.prepare(
    `SELECT ${fields} FROM submissions WHERE ${where}
     ORDER BY max_sustained DESC, updated_at DESC LIMIT 3`).bind(profile, gpu).all();
  const { results: recent } = await env.DB.prepare(
    `SELECT ${fields} FROM submissions WHERE ${where}
     ORDER BY updated_at DESC LIMIT 10`).bind(profile, gpu).all();
  return jsonPub({ gpu, profile, dist, top, recent });
}

async function handleProfiles(env) {
  // which streaming boards exist (source→output codec pairs) + how many runs each
  const { results } = await env.DB.prepare(
    `SELECT profile, COUNT(*) AS count FROM submissions WHERE hidden = 0
     GROUP BY profile ORDER BY count DESC`).all();
  // canonical always listed (and first) even before it has submissions
  if (!results.some(r => r.profile === CANONICAL)) results.unshift({ profile: CANONICAL, count: 0 });
  else results.sort((a, b) => (a.profile === CANONICAL ? -1 : b.profile === CANONICAL ? 1 : b.count - a.count));
  return jsonPub({ profiles: results, canonical: CANONICAL });
}

// Admin moderation: token in the Authorization header (NEVER a query string — URLs leak into
// histories/logs), integer-validated id, 404 on missing, every action audited, restorable.
//   POST /api/admin/hide?id=<n>[&reason=...]     Authorization: Bearer <ADMIN_TOKEN>
//   POST /api/admin/restore?id=<n>[&reason=...]  Authorization: Bearer <ADMIN_TOKEN>
async function handleAdmin(request, url, env, action) {
  const auth = request.headers.get("Authorization") || "";
  if (!env.ADMIN_TOKEN || auth !== "Bearer " + env.ADMIN_TOKEN) return bad("forbidden", 403);
  const id = parseInt(url.searchParams.get("id") || "", 10);
  if (!Number.isInteger(id) || id < 1) return bad("bad id");
  const row = await env.DB.prepare("SELECT id, hidden FROM submissions WHERE id = ?").bind(id).first();
  if (!row) return bad("not found", 404);
  const hidden = action === "hide" ? 1 : 0;
  await env.DB.prepare("UPDATE submissions SET hidden = ? WHERE id = ?").bind(hidden, id).run();
  const reason = (url.searchParams.get("reason") || "").slice(0, 200) || null;
  await env.DB.prepare(
    "INSERT INTO moderation_actions (submission_id, action, reason, created_at) VALUES (?,?,?,?)")
    .bind(id, action, reason, Math.floor(Date.now() / 1000)).run();
  return json({ ok: true, id, action });
}

// ---- the public page ---------------------------------------------------------------------------
const PAGE = `<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>GPU Transcode Benchmark — Leaderboard</title><style>
:root{--bg:#0a0e14;--panel:#121823;--ink:#e8eef7;--muted:#7b8aa0;--accent:#4aa3ff;--green:#2ecc71}
*{box-sizing:border-box;margin:0;padding:0}
body{background:radial-gradient(1000px 700px at 50% 0%,#10243b 0%,#06101c 70%);color:var(--ink);
  font:16px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;min-height:100vh;padding:40px 16px}
.wrap{max-width:860px;margin:0 auto;text-align:center}
.cap{color:var(--muted);font-size:15px;letter-spacing:4px;text-transform:uppercase}
h1{font-size:44px;font-weight:900;letter-spacing:-1px;margin:6px 0 4px}
.sub{color:var(--muted);margin-bottom:28px}.sub b{color:var(--ink)}
table{width:100%;border-collapse:collapse;background:#0e1521;border:1px solid #25405d;border-radius:14px;overflow:hidden}
th{font-size:11px;letter-spacing:.08em;text-transform:uppercase;color:var(--muted);padding:12px 14px;text-align:left;border-bottom:1px solid #1a2738}
td{padding:12px 14px;text-align:left;border-bottom:1px solid #141d2c;font-size:15px}
tr:last-child td{border-bottom:none}
td.rank{color:var(--muted);width:44px}tr.top td.rank,tr.top td.gpu{color:var(--accent);font-weight:800}
td.gpu{font-weight:700}td.num{font-variant-numeric:tabular-nums}
td .v{font-size:11px;letter-spacing:.06em;text-transform:uppercase;color:var(--muted)}
.big{font-weight:800;font-size:17px}.cap2{color:var(--muted);font-size:12.5px}
.range{color:var(--muted);font-size:12.5px}
tr.gpurow{cursor:pointer}tr.gpurow:hover td{background:#111b2b}
.chev{display:inline-block;color:var(--muted);transition:transform .15s;margin-right:6px}
tr.open .chev{transform:rotate(90deg)}
tr.detail>td{background:#0a111d;padding:18px 22px}
.dwrap{display:flex;gap:28px;flex-wrap:wrap;align-items:flex-start;text-align:left}
.dcol{flex:1;min-width:250px}
.dhead{font-size:11px;letter-spacing:.08em;text-transform:uppercase;color:var(--muted);margin-bottom:8px}
.dots{width:100%;height:84px;display:block}
.hbar{fill:var(--accent);opacity:.85}.daxis{stroke:#25405d;stroke-width:1}
.medline{stroke:#f1c40f;stroke-width:1;stroke-dasharray:4 3}
.dlbl{fill:#7b8aa0;font-size:10px;text-anchor:middle}
.hitbox{cursor:pointer}
.legend{display:flex;gap:14px;font-size:11px;color:var(--muted);margin-top:4px;flex-wrap:wrap}
.legend i{display:inline-block;width:9px;height:9px;border-radius:2px;margin-right:5px}
.barinfo{margin-top:8px;font-size:12.5px;color:#cdd9e8;background:#111b2b;border:1px solid #1e3048;border-radius:8px;padding:8px 12px;line-height:1.45}
.punch{margin-top:10px;font-size:13.5px;font-weight:700;color:var(--green);line-height:1.4}
.ccount{color:var(--muted);font-size:12px}
.prov{color:#f1c40f;font-size:11px;letter-spacing:.05em;text-transform:uppercase;margin-left:6px}
.under{color:#f1c40f;font-size:12.5px;margin-top:3px}
.allline{color:var(--muted);font-size:12.5px;margin-top:3px}
.vtoggle{display:flex;justify-content:flex-end;margin:0 0 10px}
.vtoggle button{background:#0e1928;border:1px solid #2c3e55;border-radius:999px;padding:6px 14px;font-size:12.5px;color:var(--muted);cursor:pointer}
.vtoggle button.on{background:var(--accent);color:#04121f;font-weight:700;border-color:var(--accent)}
.lbadge{display:inline-block;font-size:10px;letter-spacing:.05em;text-transform:uppercase;border-radius:5px;padding:1px 6px;margin-left:4px}
.lbadge.throughput{background:#123524;color:#2ecc71}.lbadge.session{background:#332b10;color:#f1c40f}
.lbadge.memory{background:#301a34;color:#c77dff}.lbadge.unknown{background:#3a1414;color:#ff7675}
.ramrow{display:flex;justify-content:space-between;font-size:14px;padding:5px 0;border-bottom:1px solid #141d2c}
.ramrow:last-child{border-bottom:none}.ramrow b{font-variant-numeric:tabular-nums}
.ramnote{font-size:12px;color:var(--muted);margin-top:8px;line-height:1.45}
.runrow{font-size:12.5px;color:var(--muted);padding:3px 0}.runrow b{color:#cdd9e8}
.empty{padding:40px;color:var(--muted)}
.foot{margin-top:22px;color:var(--muted);font-size:13px}.foot a{color:var(--accent);text-decoration:none}
.profs{display:flex;gap:8px;justify-content:center;flex-wrap:wrap;margin:0 0 20px}
.prof{background:#0e1928;border:1px solid #2c3e55;border-radius:999px;padding:7px 16px;font-size:13.5px;color:var(--muted);cursor:pointer}
.prof:hover{color:var(--ink);border-color:var(--accent)}
.prof.on{background:var(--accent);color:#04121f;font-weight:700;border-color:var(--accent)}
.prof small{opacity:.75;margin-left:5px}
</style></head><body><div class="wrap">
<div class="cap">GPU Transcode Benchmark</div><h1>Leaderboard</h1>
<div class="sub" id="sub">Simultaneous <b>4K HEVC → 1080p H.264 (8M)</b> streams at ≥ 1.0× realtime · median of community submissions · click a row for the breakdown</div>
<div class="profs" id="profs"></div>
<div class="vtoggle"><button id="vt" onclick="toggleView()">Show all runs &amp; failure detail</button></div>
<table id="t"><thead><tr><th></th><th>GPU</th><th>Streams (median)</th><th>Best</th><th>≈W/stream</th><th>Runs</th></tr></thead>
<tbody id="tb"><tr><td colspan="6" class="empty">Loading…</td></tr></tbody></table>
<div class="foot">Run it on your own Unraid server — search <b>GPU Stream Benchmark</b> in Community Apps.</div>
</div><script>
const esc=s=>String(s??"").replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const median=a=>{const s=[...a].sort((x,y)=>x-y);return s.length?(s.length%2?s[(s.length-1)/2]:(s[s.length/2-1]+s[s.length/2])/2):null};
// weighted median over [{v, c}] pairs — exact, from the GROUP BY distribution
function wmedian(pairs){
  const s=[...pairs].sort((a,b)=>a.v-b.v);
  const total=s.reduce((t,p)=>t+p.c,0);
  if(!total) return null;
  let seen=0, lo=null, hi=null;
  const li=(total-1)/2, hiI=total/2;   // indexes of the middle element(s), 0-based
  for(const p of s){
    if(lo===null && seen+p.c>Math.floor(li)) lo=p.v;
    if(hi===null && seen+p.c>Math.floor(total%2?li:hiI)) hi=p.v;
    seen+=p.c;
  }
  return total%2 ? lo : (lo+hi)/2;
}
// RAM generation buckets — few enough for colour-coding; exact strings stay in tables/click-detail
const FAMS=["DDR5","DDR4","DDR3","other"];
const FAM_COLORS={DDR5:"#2ecc71",DDR4:"#4aa3ff",DDR3:"#f1c40f",other:"#55657d"};
const fam=r=>{const u=String(r||"").toUpperCase();
  return u.startsWith("DDR5")||u.startsWith("LPDDR5")?"DDR5"
       : u.startsWith("DDR4")||u.startsWith("LPDDR4")?"DDR4"
       : u.startsWith("DDR3")?"DDR3":"other";};
let BAR_INFO={};   // streams → exact-RAM breakdown html for the clicked bar
function barClick(v){
  const el=document.getElementById("barinfo");
  el.innerHTML=BAR_INFO[v]||""; el.style.display=BAR_INFO[v]?"block":"none";
}
function histogram(dist, med){
  // stacked bars: streams (x) × count (height), segments coloured by RAM generation, so the
  // "faster RAM sits in the taller bars" correlation is visible with no interaction at all.
  // Clicking a bar shows that score's exact RAM makeup.
  const perV={};                                   // v → {fam → count}
  for(const x of dist){ const v=x.streams; (perV[v]=perV[v]||{})[fam(x.ram)]=(perV[v]&&perV[v][fam(x.ram)]||0)+x.count; }
  BAR_INFO={};
  const exact={};                                  // v → {ramString → count} for the click detail
  for(const x of dist){ const v=x.streams, k=x.ram||"no RAM data"; (exact[v]=exact[v]||{})[k]=(exact[v][k]||0)+x.count; }
  for(const v of Object.keys(exact)){
    const parts=Object.entries(exact[v]).sort((a,b)=>b[1]-a[1])
      .map(([k,c])=>esc(k)+(c>1?" ×"+c:"")).join(" · ");
    BAR_INFO[v]='<b>'+v+' streams</b> — '+parts;
  }
  const ks=Object.keys(perV).map(Number), mn=Math.min(...ks), mx=Math.max(...ks);
  const W=320,H=84,p=14,base=H-18,topPad=14;
  const span=Math.max(1,mx-mn), bw=Math.min(26,(W-2*p)/(span+1)-3);
  const X=v=>mn===mx?W/2:p+(v-mn)/span*(W-2*p-bw);
  const cmax=Math.max(...ks.map(v=>Object.values(perV[v]).reduce((t,c)=>t+c,0)));
  let svg='<line class="daxis" x1="'+p+'" y1="'+base+'" x2="'+(W-p)+'" y2="'+base+'"/>';
  const famsPresent=new Set();
  for(let v=mn;v<=mx;v++){
    const segs=perV[v]; if(!segs) continue;
    const total=Object.values(segs).reduce((t,c)=>t+c,0);
    const hTot=Math.max(3,(total/cmax)*(base-topPad-12));
    let y=base;
    for(const f of FAMS){
      const c=segs[f]||0; if(!c) continue;
      famsPresent.add(f);
      const h=hTot*(c/total); y-=h;
      svg+='<rect x="'+X(v)+'" y="'+y+'" width="'+bw+'" height="'+h+'" rx="1.5" fill="'+FAM_COLORS[f]+'" opacity=".9"/>';
    }
    svg+='<text class="dlbl" x="'+(X(v)+bw/2)+'" y="'+(y-4)+'">'+total+'</text>';
    svg+='<text class="dlbl" x="'+(X(v)+bw/2)+'" y="'+(H-4)+'">'+v+'</text>';
    svg+='<rect class="hitbox" x="'+(X(v)-1.5)+'" y="'+topPad+'" width="'+(bw+3)+'" height="'+(base-topPad)
      +'" fill="transparent" onclick="barClick('+v+')"/>';
  }
  if(med!=null) svg+='<line class="medline" x1="'+(X(Math.round(med))+bw/2)+'" y1="'+(topPad-6)
    +'" x2="'+(X(Math.round(med))+bw/2)+'" y2="'+base+'"/>';
  let legend="";
  const lbl={DDR5:"DDR5",DDR4:"DDR4",DDR3:"DDR3",other:"no RAM data"};
  if(famsPresent.size>1 || !famsPresent.has("other"))
    legend='<div class="legend">'+FAMS.filter(f=>famsPresent.has(f))
      .map(f=>'<span><i style="background:'+FAM_COLORS[f]+'"></i>'+lbl[f]+'</span>').join("")+'</div>';
  return '<svg class="dots" viewBox="0 0 '+W+' '+H+'" preserveAspectRatio="xMidYMid meet">'+svg+'</svg>'
    +legend+'<div id="barinfo" class="barinfo" style="display:none"></div>';
}
function runLine(r){
  const lr = r.limit_reason;
  return '<b>'+r.max_sustained+' streams</b>'
    +(lr?'<span class="lbadge '+esc(lr)+'">'+esc(lr==="memory"?"VRAM wall":lr==="session"?"session cap":lr)+'</span>':'')
    +(r.capped&&r.projected?' · throughput ≈'+r.projected+'×':'')
    +(r.ram?' · '+esc(r.ram):'')+(r.cpu?' · '+esc(r.cpu):'')+(r.driver?' · '+esc(r.driver):'')
    +(r.os_version?' · Unraid '+esc(r.os_version):'')
    +(r.updated_at?' · '+new Date(r.updated_at*1000).toLocaleDateString(undefined,{month:"short",day:"numeric"}):'');
}
function detailHtml(d){
  const dist=d.dist||[];
  if(!dist.length) return '<div class="dhead">No runs</div>';
  const total=dist.reduce((t,x)=>t+x.count,0);
  const hist=new Map();
  for(const x of dist) hist.set(x.streams,(hist.get(x.streams)||0)+x.count);
  const med=wmedian([...hist.entries()].map(([v,c])=>({v,c})));
  // RAM grouping — exact weighted medians from the distribution; absent for dGPUs (no ram)
  const groups={};
  for(const x of dist){ if(x.ram) (groups[x.ram]=groups[x.ram]||[]).push({v:x.streams,c:x.count}); }
  const gl=Object.entries(groups).map(([ram,pairs])=>({ram,med:wmedian(pairs),
    n:pairs.reduce((t,p)=>t+p.c,0)})).sort((a,b)=>b.med-a.med);
  const ramRows=gl.map(g=>'<div class="ramrow"><span>'+esc(g.ram)+'</span><span><b>'+g.med
    +'</b> <span class="range">('+g.n+' run'+(g.n>1?'s':'')+')</span></span></div>').join("");
  // the buyer's punchline: how much the fastest RAM tier buys over the slowest
  let punch="";
  if(gl.length>1 && gl[gl.length-1].med>0){
    const pct=Math.round((gl[0].med/gl[gl.length-1].med-1)*100);
    if(pct>=10) punch='<div class="punch">'+esc(gl[0].ram)+' gets ~'+pct+'% more streams than '
      +esc(gl[gl.length-1].ram)+' on this GPU.</div>';
  }
  const top=(d.top||[]).map((r,i)=>'<div class="runrow">'+["①","②","③"][i]+' '+runLine(r)+'</div>').join("");
  const recent=(d.recent||[]).map(r=>'<div class="runrow">'+runLine(r)+'</div>').join("");
  return '<div class="dwrap">'
    +'<div class="dcol"><div class="dhead">Distribution — '+total+' submission'+(total>1?'s':'')
      +' (median marked'+(total>1?' · click a bar for its RAM makeup':'')+')</div>'
      +histogram(dist,med)+'</div>'
    +(ramRows&&total>=3?'<div class="dcol"><div class="dhead">Median by RAM speed</div>'+ramRows+punch
      +'<div class="ramnote">This GPU shares system RAM as its video memory — memory speed directly moves this score (enable XMP/EXPO!).</div></div>':'')
    +'<div class="dcol"><div class="dhead">Fastest systems</div>'+top
      +'<div class="dhead" style="margin-top:14px">Recent submissions</div>'+recent+'</div></div>';
}
let PROFILE = "4K HEVC -> 1080p H264";   // current board (canonical by default)
const CODEC_NICE = {H264:"H.264", HEVC:"HEVC", AV1:"AV1"};
function profLabel(p){
  // "4K AV1 -> 1080p H264" → "4K AV1 → 1080p H.264". Order matters: prettify BEFORE esc()
  // (esc turns "->" into "-&gt;"); \\b because this lives inside a template literal, where a
  // single \b is a backspace escape, not a regex word boundary.
  return esc(p.replace("->","→").replace(/\\b(H264|HEVC|AV1)\\b/g, m=>CODEC_NICE[m]||m));
}
async function toggle(tr, gpu, gen, cap){
  const open=tr.classList.contains("open");
  document.querySelectorAll("tr.detail").forEach(e=>e.remove());
  document.querySelectorAll("tr.open").forEach(e=>e.classList.remove("open"));
  if(open) return;
  tr.classList.add("open");
  const det=document.createElement("tr"); det.className="detail";
  det.innerHTML='<td colspan="6">Loading…</td>';
  tr.after(det);
  try{
    const d=await (await fetch("/api/detail?gpu="+encodeURIComponent(gpu)
      +"&profile="+encodeURIComponent(PROFILE)+(gen?("&gen="+encodeURIComponent(gen)):"")
      +"&cap="+(cap||"0"))).json();
    det.firstChild.innerHTML=detailHtml(d);
  }catch(e){ det.firstChild.textContent="Could not load details."; }
}
let ALLVIEW=false, ROWS=[];
function toggleView(){
  ALLVIEW=!ALLVIEW;
  document.getElementById("vt").classList.toggle("on",ALLVIEW);
  renderRows();
}
function renderRows(){
  const tb=document.getElementById("tb");
  if(!ROWS.length){tb.innerHTML='<tr><td colspan="6" class="empty">No submissions yet for this test — be the first!</td></tr>';return;}
  // ranking is IDENTICAL in both view states — the toggle adds depth, never re-sorts
  tb.innerHTML=ROWS.map((r,i)=>{
    const cnt = r.understated ? (r.count+' run'+(r.count>1?'s':'')) : (r.clean_count+' clean run'+(r.clean_count>1?'s':''));
    return '<tr class="gpurow'+(i===0?' top':'')+'" data-gpu="'+esc(r.base_gpu)+'" data-gen="'+esc(r.ram_gen||"")+'" data-cap="'+(r.session_capped?'1':'0')+'"><td class="rank">'+(i+1)+'</td>'
      +'<td class="gpu"><span class="chev">▸</span>'+esc(r.gpu)+'<div class="v">'+esc(r.vendor||"")+'</div></td>'
      +'<td class="num"><span class="big">'+r.median_streams+'</span>'
      +' <span class="ccount">('+cnt+')</span>'
      +(r.count>1&&r.min_streams!==r.best_streams?' <span class="range">('+r.min_streams+'–'+r.best_streams+')</span>':'')
      +(r.provisional?'<span class="prov">provisional</span>':'')
      +(r.understated?'<div class="under">measured under load — may understate</div>':'')
      +(ALLVIEW&&!r.understated&&r.all_count>r.clean_count?'<div class="allline">all runs: median '+r.all_median+' ('+r.all_count+')</div>':'')
      +(r.mostly_capped?'<div class="cap2">engine throughput ≈'+(r.median_projected||"?")+'× realtime — sessions capped by the driver</div>':'')+'</td>'
      +'<td class="num">'+r.best_streams+'</td>'
      +'<td class="num">'+(r.median_wps!=null?r.median_wps:"—")+'</td>'
      +'<td class="num">'+r.count+'</td></tr>';
  }).join("");
  tb.querySelectorAll("tr.gpurow").forEach(tr=>tr.addEventListener("click",()=>toggle(tr,tr.dataset.gpu,tr.dataset.gen,tr.dataset.cap)));
}
function loadBoard(){
  document.getElementById("sub").innerHTML='Simultaneous <b>'+profLabel(PROFILE)
    +' (8M)</b> streams at ≥ 1.0× realtime · median of clean-start community runs · click a row for the breakdown';
  document.querySelectorAll(".prof").forEach(b=>b.classList.toggle("on", b.dataset.p===PROFILE));
  const tb=document.getElementById("tb");
  tb.innerHTML='<tr><td colspan="6" class="empty">Loading…</td></tr>';
  fetch("/api/top?profile="+encodeURIComponent(PROFILE)).then(r=>r.json()).then(d=>{
    ROWS=d.rows||[]; renderRows();
  });
}
fetch("/api/profiles").then(r=>r.json()).then(d=>{
  const el=document.getElementById("profs");
  const ps=(d.profiles||[]);
  // pills only earn their place once there is a CHOICE to make
  if(ps.length>1){
    el.innerHTML=ps.map(p=>'<button class="prof" data-p="'+esc(p.profile)+'">'+profLabel(p.profile)
      +(p.count?'<small>'+p.count+'</small>':'')+'</button>').join("");
    el.querySelectorAll(".prof").forEach(b=>b.addEventListener("click",()=>{PROFILE=b.dataset.p;loadBoard();}));
  }
  loadBoard();
});
</script></body></html>`;

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    const p = url.pathname;
    // NO blanket preflight approval: the POST routes are not for browsers (the container posts
    // server-side, where CORS doesn't apply). GET routes never need preflight. An OPTIONS
    // request gets no CORS headers → cross-origin browser POSTs are refused by the browser.
    if (request.method === "OPTIONS") return new Response(null, { status: 405 });
    if (p === "/api/submit" && request.method === "POST") return handleSubmit(request, env);
    if (p === "/api/top" && request.method === "GET") return handleTop(url, env);
    if (p === "/api/detail" && request.method === "GET") return handleDetail(url, env);
    if (p === "/api/profiles" && request.method === "GET") return handleProfiles(env);
    if (p === "/api/admin/hide" && request.method === "POST") return handleAdmin(request, url, env, "hide");
    if (p === "/api/admin/restore" && request.method === "POST") return handleAdmin(request, url, env, "restore");
    if (p === "/" || p === "/index.html")
      return new Response(PAGE, { headers: { "Content-Type": "text/html; charset=utf-8",
        "Cache-Control": "no-store",
        // the page is fully self-contained (inline script/style, same-origin fetches only)
        "Content-Security-Policy": "default-src 'none'; script-src 'unsafe-inline'; " +
          "style-src 'unsafe-inline'; connect-src 'self'; img-src 'self' data:; " +
          "base-uri 'none'; form-action 'none'; frame-ancestors 'none'",
        "X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer" } });
    return bad("not found", 404);
  }
};
