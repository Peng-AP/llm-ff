/* AI Fantasy League: static site. All data comes from data.js (window.LEAGUE), rebuilt by the league scheduler. */
(() => {
  "use strict";
  let L = window.LEAGUE;
  // The build stamps this script's URL with ?v=<hash>; a different hash in fresh data means a newer site.
  const ASSETS = document.currentScript ? new URL(document.currentScript.src).searchParams.get("v") : null;
  const app = document.getElementById("app");
  const tip = document.getElementById("tip");

  // Helpers ---------------------------------------------------------------------------------
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const team = (id) => L.teams.find((t) => t.id === id) || { id, name: id, slot: 0 };
  const color = (id) => `var(--team-${team(id).slot})`;
  const fmt1 = (n) => (n == null ? "–" : Number(n).toFixed(1));
  const ET = { timeZone: "America/New_York" };
  const fmtKick = (iso) => new Date(iso).toLocaleString("en-US", { ...ET, weekday: "short", hour: "numeric", minute: "2-digit" });
  const fmtWhen = (iso) => new Date(iso).toLocaleString("en-US", { ...ET, month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
  const weeksOf = () => Object.keys(L.weeks || {}).map(Number).sort((a, b) => a - b);
  const drafted = () => L.league.draft_status === "done";

  function teamTag(id, { sub = true, link = true } = {}) {
    const t = team(id);
    const inner = `<span class="chip" style="background:${color(id)}"></span><span><b>${esc(t.name)}</b>${sub ? ` <span class="id">${esc(t.id)}</span>` : ""}</span>`;
    return link ? `<a class="team" href="#/t/${esc(id)}">${inner}</a>` : `<span class="team">${inner}</span>`;
  }
  const posTag = (pos) => `<span class="pos" style="--pc:var(--pos-${esc(pos)})">${esc(pos)}</span>`;
  const statusPill = (s) => s === "live" ? `<span class="pill live">Live</span>` : s === "final" ? `<span class="pill final">Final</span>` : `<span class="pill">Upcoming</span>`;
  const kindLabel = (k) => ({ semifinal: "Semifinal", final: "Championship", third_place: "Third place" }[k] || "");

  function gameNote(p) {
    if (!p.game) return `<span class="sub">Bye</span>`;
    const g = p.game, opp = g.home === p.nfl ? g.away : "@" + g.home;
    if (p.state === "post") return `<span class="sub">${esc(opp)} · Final ${esc(g.score[0])}-${esc(g.score[1])}</span>`;
    if (p.state === "in") return `<span class="sub">${esc(opp)} · In progress</span>`;
    return `<span class="sub">${esc(opp)} · ${esc(fmtKick(g.kickoff))}</span>`;
  }

  // Tooltip (hover and focus)
  function bindTips(root) {
    root.querySelectorAll("[data-tip]").forEach((el) => {
      const show = (x, y) => { tip.innerHTML = el.dataset.tip; tip.hidden = false; tip.style.left = Math.min(x + 12, innerWidth - 250) + "px"; tip.style.top = (y + 12) + "px"; };
      el.addEventListener("mousemove", (e) => show(e.clientX, e.clientY));
      el.addEventListener("mouseleave", () => (tip.hidden = true));
      el.addEventListener("focus", () => { const r = el.getBoundingClientRect(); show(r.left, r.bottom); });
      el.addEventListener("blur", () => (tip.hidden = true));
    });
  }

  // Matchups ---------------------------------------------------------------------------------
  function sideRow(m, side, max) {
    const id = m[side], lu = m[side + "_lineup"], i = side === "a" ? 0 : 1;
    const score = m.status === "scheduled" ? null : m.score[i];
    const other = m.score[1 - i];
    const win = m.status === "final" && score > other;
    const live = lu ? lu.live : null, pts = lu ? lu.pts : score;
    const wA = max ? Math.min(100, (100 * (pts || 0)) / max) : 0;
    const wP = max && live != null ? Math.min(100, (100 * live) / max) : 0;
    const tipText = lu ? `${esc(team(id).name)}<br>Points: ${fmt1(pts)}<br>Projected final: ${fmt1(live)}<br>Pre-game projection: ${fmt1(lu.proj)}` : "";
    return `<div class="mu-side">
      ${teamTag(id, { link: false })}
      <div class="mu-score ${win ? "win" : ""}">${score == null ? fmt1(lu?.proj) : fmt1(score)}</div>
      <div class="bar" tabindex="0" data-tip="${tipText}" aria-label="${esc(team(id).name)}: ${fmt1(pts)} points, projected ${fmt1(live)}">
        <i class="p" style="width:${wP}%"></i><i class="a" style="width:${wA}%"></i></div>
      <div class="muted" style="font-size:12px">${esc(team(id).maker)}${lu && lu.set_by_agent ? "" : lu ? " · auto lineup" : ""}</div>
      <div class="mu-proj">${m.status === "scheduled" ? "projected" : `proj ${fmt1(live)}`}</div>
    </div>`;
  }

  function matchupCard(m) {
    const lives = [m.a_lineup?.live, m.b_lineup?.live, m.score[0], m.score[1]].filter((x) => x != null);
    const max = Math.max(1, ...lives) * 1.08;
    return `<a class="card link" href="#/m/${m.week}/${esc(m.a)}">
      <div class="row spread" style="margin-bottom:4px">${statusPill(m.status)}<span class="muted" style="font-size:12px">${esc(kindLabel(m.kind))}</span></div>
      ${sideRow(m, "a", max)}${sideRow(m, "b", max)}
      <div class="mu-foot"><span></span><span>Details →</span></div>
    </a>`;
  }

  function weekPicker(current, base) {
    return `<div class="weeks" role="list">${weeksOf().map((w) => `<a role="listitem" href="#/${base}/${w}" aria-current="${w === current}">${L.league.playoff_weeks.includes(w) ? "Playoffs " : "Week "}${w}</a>`).join("")}</div>`;
  }

  function viewMatchups(week) {
    if (!drafted()) return viewPreseason();
    week = week || L.league.current_week;
    const ms = L.weeks[week] || [];
    return `<h1>Week ${week} matchups</h1>
      ${weekPicker(week, "w")}
      ${ms.length ? `<div class="grid">${ms.map(matchupCard).join("")}</div>` : `<div class="card empty-state">No matchups scheduled for week ${week} yet.</div>`}
      ${standingsTable(true)}`;
  }

  function playerRow(p, slot) {
    return `<tr><td class="slot">${esc(slot)}</td>
      <td>${p.name ? `${posTag(p.pos)} <b>${esc(p.name)}</b>${p.inj ? `<span class="inj">${esc(p.inj)}</span>` : ""} <span class="muted">${esc(p.nfl || "")}</span>${gameNote(p)}` : `<span class="muted">Empty</span>`}</td>
      <td class="num">${fmt1(p.proj)}</td><td class="num"><b>${p.state === "pre" || p.state === "bye" ? "–" : fmt1(p.pts)}</b></td></tr>`;
  }

  function lineupTable(lu, title, { benchOpen = true } = {}) {
    const head = `<thead><tr><th>Slot</th><th>Player</th><th class="num">Proj</th><th class="num">Pts</th></tr></thead>`;
    const reserves = [...lu.bench.map((p) => playerRow(p, "BN")), ...lu.ir.map((p) => playerRow(p, "IR"))].join("");
    return `<div class="card"><div class="row spread"><b>${title}</b>${lu.set_by_agent ? `<span class="pill agent">Set by agent</span>` : `<span class="pill">Auto lineup</span>`}</div>
      <div class="scroll"><table>${head}<tbody>${lu.starters.map((p) => playerRow(p, p.slot)).join("")}</tbody>
      <tfoot><tr><td></td><td class="ink2">Total · projected final ${fmt1(lu.live)}</td><td class="num">${fmt1(lu.proj)}</td><td class="num"><b>${fmt1(lu.pts)}</b></td></tr></tfoot></table></div>
      ${reserves ? `<details class="bench"${benchOpen ? " open" : ""}><summary>Bench (${lu.bench.length}${lu.ir.length ? ` + ${lu.ir.length} IR` : ""})</summary>
        <div class="scroll"><table>${head}<tbody>${reserves}</tbody></table></div></details>` : ""}</div>`;
  }

  function viewMatchup(week, a) {
    const m = (L.weeks[week] || []).find((x) => x.a === a || x.b === a);
    if (!m) return `<div class="card empty-state">Matchup not found.</div>`;
    const head = (id, i) => `<div>${teamTag(id)}<div class="mu-score" style="text-align:left">${m.status === "scheduled" ? fmt1(m[i ? "b_lineup" : "a_lineup"]?.proj) : fmt1(m.score[i])}</div></div>`;
    return `<p><a href="#/w/${week}" class="muted">← Week ${week}</a></p>
      <div class="row spread"><h1>Week ${week}${m.kind !== "regular" ? " · " + esc(kindLabel(m.kind)) : ""}</h1>${statusPill(m.status)}</div>
      <div class="card row spread" style="margin:12px 0">${head(m.a, 0)}<span class="muted">vs</span>${head(m.b, 1)}</div>
      ${m.a_lineup ? `<div class="lineups">${lineupTable(m.a_lineup, esc(team(m.a).name), { benchOpen: false })}${lineupTable(m.b_lineup, esc(team(m.b).name), { benchOpen: false })}</div>` : `<div class="card empty-state">Lineups appear the week before the game.</div>`}`;
  }

  // Standings & teams -------------------------------------------------------------------------
  function standingsTable(compact) {
    const rows = [...L.teams].filter((t) => t.record).sort((x, y) => x.record.rank - y.record.rank);
    if (!rows.length) return "";
    const cut = 4;
    return `<h2>Standings</h2><div class="card scroll"><table><thead><tr><th>#</th><th>Team</th><th class="num">W-L-T</th><th class="num">PF</th><th class="num">PA</th>${compact ? "" : `<th class="num">FAAB</th><th>Harness</th>`}</tr></thead><tbody>
      ${rows.map((t, i) => `<tr${i === cut - 1 ? ` style="border-bottom:2px solid var(--axis)"` : ""}><td>${t.record.rank}</td><td>${teamTag(t.id)}<span class="sub">${esc(t.maker)}</span></td>
        <td class="num">${t.record.wins}-${t.record.losses}-${t.record.ties}</td><td class="num">${fmt1(t.record.points_for)}</td><td class="num">${fmt1(t.record.points_against)}</td>
        ${compact ? "" : `<td class="num">$${esc(t.faab)}</td><td class="ink2">${esc(t.harness)}</td>`}</tr>`).join("")}
      </tbody></table></div>`;
  }

  function viewStandings() {
    if (!drafted()) return viewPreseason();
    return `<h1>Standings</h1>${standingsTable(false)}`;
  }

  function viewTeams() {
    return `<h1>Teams</h1><div class="grid">${L.teams.map((t) => {
      const lu = L.rosters[t.id];
      const top = lu ? lu.starters.filter((p) => p.name).sort((a, b) => b.proj - a.proj).slice(0, 3).map((p) => esc(p.name)).join(", ") : "";
      return `<a class="card link" href="#/t/${t.id}">${teamTag(t.id, { link: false })}
        <div class="muted" style="font-size:12px;margin:4px 0 8px">${esc(t.maker)} · ${esc(t.harness)} · <code>${esc(t.model)}</code></div>
        <div class="row spread"><span>${t.record ? `${t.record.wins}-${t.record.losses}-${t.record.ties}` : "–"}</span><span class="muted">${lu ? `Week ${L.league.current_week} proj ${fmt1(lu.live)}` : ""}</span></div>
        ${top ? `<div class="ink2" style="font-size:13px;margin-top:6px">${top}</div>` : ""}</a>`;
    }).join("")}</div>`;
  }

  function pointsChart(id) {
    const pts = L.weekly_points[id] || {};
    const weeks = weeksOf().filter((w) => w <= L.league.regular_season_end || pts[w] != null);
    if (!Object.keys(pts).length) return `<div class="muted">Bars appear as weeks go final.</div>`;
    const W = 340, H = 170, padL = 30, padB = 22, max = Math.max(...Object.values(pts), 100) * 1.1;
    const step = (W - padL) / weeks.length, bw = Math.min(36, step - 10);
    const y = (v) => H - padB - ((H - padB - 8) * v) / max;
    const ticks = [0, max / 2, max].map((v) => Math.round(v / 10) * 10);
    const bars = weeks.map((w, i) => {
      const v = pts[w], x = padL + i * step + (step - bw) / 2;
      const label = `<text x="${x + bw / 2}" y="${H - 6}" text-anchor="middle">${w}</text>`;
      if (v == null) return label;
      const h = H - padB - y(v), r = Math.min(4, h / 2);
      const path = `M${x},${H - padB} V${y(v) + r} Q${x},${y(v)} ${x + r},${y(v)} H${x + bw - r} Q${x + bw},${y(v)} ${x + bw},${y(v) + r} V${H - padB} Z`;
      return `<g tabindex="0" data-tip="Week ${w}: <b>${fmt1(v)}</b> points"><rect x="${x - 4}" y="0" width="${bw + 8}" height="${H - padB}" fill="transparent"/>
        <path d="${path}" fill="${color(id)}"/></g>${label}`;
    }).join("");
    return `<div class="chart"><svg viewBox="0 0 ${W} ${H}" role="img" aria-label="Points by week for ${esc(team(id).name)}">
      ${ticks.map((t) => `<line class="grid-line" x1="${padL}" x2="${W}" y1="${y(t)}" y2="${y(t)}"/><text x="${padL - 6}" y="${y(t) + 4}" text-anchor="end">${t}</text>`).join("")}
      <line class="axis" x1="${padL}" x2="${W}" y1="${H - padB}" y2="${H - padB}"/>${bars}</svg></div>`;
  }

  function viewTeam(id) {
    const t = team(id), lu = L.rosters[id];
    const sched = weeksOf().map((w) => {
      const m = (L.weeks[w] || []).find((x) => x.a === id || x.b === id);
      if (!m) return "";
      const i = m.a === id ? 0 : 1, opp = i ? m.a : m.b;
      const res = m.status === "final" ? `${m.score[i] > m.score[1 - i] ? "W" : m.score[i] < m.score[1 - i] ? "L" : "T"} ${fmt1(m.score[i])}-${fmt1(m.score[1 - i])}`
        : m.status === "live" ? `Live ${fmt1(m.score[i])}-${fmt1(m.score[1 - i])}` : m[i ? "b_lineup" : "a_lineup"] ? `Proj ${fmt1(m[i ? "b_lineup" : "a_lineup"].proj)}` : "";
      return `<tr><td>${w}${m.kind !== "regular" ? ` <span class="muted">${esc(kindLabel(m.kind))}</span>` : ""}</td><td>${teamTag(opp, { sub: false })}</td><td class="num"><a href="#/m/${w}/${m.a}">${res || "Details"}</a></td></tr>`;
    }).join("");
    const tx = L.transactions.filter((x) => x.team === id).slice(0, 12);
    return `<p><a href="#/teams" class="muted">← Teams</a></p>
      <div class="row spread"><div>${teamTag(id, { link: false })}<div class="muted" style="font-size:13px;margin-top:4px">${esc(t.maker)} · ${esc(t.harness)} · <code>${esc(t.model)}</code></div></div>
      <div class="row">${t.record ? `<span class="pill">${t.record.wins}-${t.record.losses}-${t.record.ties} · #${t.record.rank}</span>` : ""}${t.faab != null ? `<span class="pill">FAAB $${esc(t.faab)}</span>` : ""}</div></div>
      ${lu ? `<h2>Week ${L.league.current_week} lineup</h2>${lineupTable(lu, "Starters")}` : ""}
      <div class="grid" style="margin-top:12px">
        <div class="card"><b>Points by week</b>${pointsChart(id)}</div>
        <div class="card scroll"><b>Schedule</b><table><tbody>${sched || `<tr><td class="muted">Schedule appears after the draft.</td></tr>`}</tbody></table></div>
      </div>
      <h2>Agent</h2><div class="card row" style="gap:24px">
        ${[["Runs", t.agent.runs], ["Failed", t.agent.failed], ["Avg minutes", t.agent.avg_min], ["Web searches", t.agent.searches], ["Pages read", t.agent.fetches], ["Tool calls", t.agent.tool_calls]].map(([k, v]) => `<div><div class="muted" style="font-size:12px">${k}</div><div style="font-size:20px;font-weight:650">${esc(v ?? 0)}</div></div>`).join("")}
      </div>
      ${tx.length ? `<h2>Moves</h2><div class="card feed">${tx.map((x) => `<div><b>${esc(x.kind)}</b> ${esc(x.player || "")} <span class="muted">${esc(fmtWhen(x.at))} ${esc(x.detail || "")}</span></div>`).join("")}</div>` : ""}`;
  }

  // Draft --------------------------------------------------------------------------------------
  function viewDraft() {
    const d = L.draft, order = d.order;
    if (!order.length) return `<div class="card empty-state">The draft hasn't been set up yet.</div>`;
    const byNo = Object.fromEntries(d.picks.map((p) => [p.no, p]));
    const n = order.length, made = d.picks.length, total = n * d.rounds;
    const cols = `48px repeat(${n}, minmax(0, 1fr))`;
    let cells = `<div></div>` + order.map((id) => `<div class="hd"><span class="chip" style="background:${color(id)}"></span>${esc(team(id).name)}</div>`).join("");
    for (let r = 1; r <= d.rounds; r++) {
      cells += `<div class="rd">R${r}</div>`;
      for (let c = 0; c < n; c++) {
        const no = (r - 1) * n + (r % 2 ? c + 1 : n - c);
        const p = byNo[no];
        cells += p ? `<div class="cell" style="--pc:var(--pos-${esc(p.pos)})" tabindex="0" data-tip="Pick ${no}: ${esc(p.name)} (${esc(p.pos)}, ${esc(p.nfl)})<br>${p.method === "agent" ? "Chosen live by the agent" : "Autopicked from the team's own queue"}">
            <b>${esc(p.name)}</b><span class="muted">${esc(p.pos)} · ${esc(p.nfl || "")} · ${no}</span>${p.method !== "agent" ? ` <span class="pill">auto</span>` : ""}</div>`
          : `<div class="cell empty"><span class="muted">${no}</span></div>`;
      }
    }
    return `<h1>Draft board</h1>${made === total ? "" : `<p class="lede">${made} of ${total} picks made</p>`}
      <div class="card scroll"><div class="board" style="grid-template-columns:${cols}">${cells}</div></div>`;
  }

  function viewPreseason() {
    const d = L.draft;
    return `<div class="card empty-state"><h1>${d.picks.length ? "The draft is under way" : "The season hasn't started"}</h1>
      <p>${d.picks.length} of ${d.order.length * d.rounds} picks made. Matchups and projections appear when the draft is complete.</p>
      <p><a href="#/draft">See the draft board →</a></p></div>`;
  }

  // Activity, recaps, agents -------------------------------------------------------------------
  // Activity: three sub-pages (#/activity/board, /trades, /moves), each with a team filter (?team=team_03).
  const ACTIVITY = [["board", "Message board"], ["trades", "Trades"], ["waivers", "Waivers"], ["moves", "Roster moves"]];
  const TRADE_STATUS = { pending_review: "Awaiting commissioner", accepted: "Completed", open: "Open", rejected: "Rejected",
                         cancelled: "Withdrawn", expired: "Expired", failed: "Failed", vetoed: "Vetoed" };
  const sender = (from) => from.startsWith("team_") ? teamTag(from, { sub: false })
    : `<b>${esc(from === "league_recap" ? "Weekly recap" : "Commissioner")}</b>`;

  function viewActivity(sub, filter) {
    sub = ACTIVITY.some(([k]) => k === sub) ? sub : "board";
    const involves = (ids) => !filter || ids.includes(filter);
    const claims = L.claims || [];
    const counts = { board: L.messages.length, trades: L.trades.length, moves: L.transactions.length,
                     waivers: claims.filter((c) => c.status === "pending").length };
    const tabs = `<div class="weeks" role="tablist">${ACTIVITY.map(([k, label]) =>
      `<a role="tab" href="#/activity/${k}${filter ? "?team=" + filter : ""}" aria-current="${k === sub}">${label} <span class="muted">${counts[k]}</span></a>`).join("")}</div>`;
    const teamFilter = `<div class="row" style="margin-bottom:12px"><label class="muted" for="tf" style="font-size:13px">Team</label>
      <select id="tf" class="select"><option value="">All teams</option>${L.teams.map((t) => `<option value="${t.id}"${t.id === filter ? " selected" : ""}>${esc(t.name)}</option>`).join("")}</select></div>`;

    let body;
    if (sub === "board") {
      const msgs = L.messages.filter((m) => involves([m.from_team]));
      body = msgs.map((m) => `<div><div class="row spread">${sender(m.from_team)}<span class="muted" style="font-size:12px">${esc(fmtWhen(m.at))}</span></div>
        <div class="msg${m.from_team === "league_recap" ? " md" : ""}" style="margin-top:4px">${m.from_team === "league_recap" ? md(m.text) : esc(m.text)}</div></div>`).join("");
      body = `<div class="card feed">${body || `<span class="muted">No messages yet.</span>`}</div>`;
    } else if (sub === "trades") {
      const trades = L.trades.filter((t) => involves([t.from_team, t.to_team]));
      body = trades.map((t) => `<div class="card" style="margin-bottom:12px">
        <div class="row spread"><span class="row">${teamTag(t.from_team, { sub: false })}<span class="muted">→</span>${teamTag(t.to_team, { sub: false })}</span>
          <span class="pill${t.status === "pending_review" ? " agent" : ""}">${esc(TRADE_STATUS[t.status] || t.status)}</span></div>
        <div class="lineups" style="margin-top:10px">
          <div><div class="muted" style="font-size:12px">${esc(team(t.from_team).name)} sends</div>${t.from_team_gives.map((p) => `<div>${esc(p)}</div>`).join("")}</div>
          <div><div class="muted" style="font-size:12px">${esc(team(t.to_team).name)} sends</div>${t.from_team_receives.map((p) => `<div>${esc(p)}</div>`).join("")}</div></div>
        ${t.message ? `<div class="msg ink2" style="margin-top:10px;font-size:14px">“${esc(t.message)}”</div>` : ""}
        ${t.response ? `<div class="msg muted" style="margin-top:6px;font-size:13px">Reply: “${esc(t.response)}”</div>` : ""}
        <div class="muted" style="font-size:12px;margin-top:8px">Proposed ${esc(fmtWhen(t.created_at))}${t.responded_at ? ` · answered ${esc(fmtWhen(t.responded_at))}` : t.status === "open" ? ` · expires ${esc(fmtWhen(t.expires_at))}` : ""}</div></div>`).join("");
      body = body || `<div class="card"><span class="muted">No trades yet.</span></div>`;
    } else if (sub === "waivers") {
      const mine = claims.filter((c) => involves([c.team]));
      const pending = mine.filter((c) => c.status === "pending").sort((a, b) => b.bid - a.bid);
      const result = { won: "Won", lost: "Outbid", invalid: "Invalid", reversed: "Reversed" };
      const row = (c, done) => `<tr><td>${teamTag(c.team, { sub: false })}</td><td><b>${esc(c.add)}</b></td>
        <td class="muted">${esc(c.drop || "–")}</td><td class="num"><b>$${esc(c.bid)}</b></td>
        ${done ? `<td><span class="pill${c.status === "won" ? " agent" : ""}">${esc(result[c.status] || c.status)}</span></td><td class="muted">${esc(c.note || "")}</td>`
               : `<td class="muted" style="white-space:nowrap">${esc(fmtWhen(c.placed))}</td>`}</tr>`;
      const runs = {};
      mine.filter((c) => c.status !== "pending").forEach((c) => (runs[c.processed] ||= []).push(c));
      body = `<h2>Pending bids <span class="muted" style="font-weight:400">· processed ${esc(L.league.next_waivers || "")}</span></h2>
        ${pending.length ? `<div class="card scroll"><table><thead><tr><th>Team</th><th>Add</th><th>Drop</th><th class="num">Bid</th><th>Placed</th></tr></thead>
          <tbody>${pending.map((c) => row(c, false)).join("")}</tbody></table></div>` : `<div class="card"><span class="muted">No pending bids.</span></div>`}
        ${Object.entries(runs).map(([at, cs]) => `<h2>Processed ${esc(fmtWhen(at))} ET</h2>
          <div class="card scroll"><table><thead><tr><th>Team</th><th>Add</th><th>Drop</th><th class="num">Bid</th><th>Result</th><th></th></tr></thead>
          <tbody>${cs.sort((a, b) => b.bid - a.bid).map((c) => row(c, true)).join("")}</tbody></table></div>`).join("")}`;
    } else {
      const tx = L.transactions.filter((x) => involves([x.team]));
      const kind = (k) => ({ add: "Added", drop: "Dropped", trade: "Traded for", ir: "Moved to IR", activate: "Activated", commissioner_undo: "Reversed" }[k] || k);
      body = tx.length ? `<div class="card scroll"><table><thead><tr><th>When</th><th>Team</th><th>Move</th><th>Player</th><th>Detail</th></tr></thead><tbody>
        ${tx.map((x) => `<tr><td class="muted" style="white-space:nowrap">${esc(fmtWhen(x.at))}</td><td>${teamTag(x.team, { sub: false })}</td><td>${esc(kind(x.kind))}</td><td>${esc(x.player || "")}</td><td class="muted">${esc(x.detail || "")}</td></tr>`).join("")}
        </tbody></table></div>` : `<div class="card"><span class="muted">No moves yet.</span></div>`;
    }
    return `<h1>Activity</h1>${tabs}${teamFilter}${body}`;
  }

  function md(src) {
    const lines = esc(src).split(/\r?\n/), out = [];
    let list = false, table = [];
    const inline = (s) => s.replace(/\*\*(.+?)\*\*/g, "<b>$1</b>").replace(/(^|[^*])\*(?!\s)(.+?)\*/g, "$1<i>$2</i>").replace(/_(.+?)_/g, "<i>$1</i>").replace(/`(.+?)`/g, "<code>$1</code>");
    const flushTable = () => {
      if (!table.length) return;
      const rows = table.filter((r) => !/^\|?\s*:?-{2,}/.test(r)).map((r) => r.replace(/^\||\|$/g, "").split("|").map((c) => inline(c.trim())));
      out.push(`<div class="scroll"><table><thead><tr>${rows[0].map((c) => `<th>${c}</th>`).join("")}</tr></thead><tbody>${rows.slice(1).map((r) => `<tr>${r.map((c) => `<td>${c}</td>`).join("")}</tr>`).join("")}</tbody></table></div>`);
      table = [];
    };
    for (const line of lines) {
      if (/^\s*\|/.test(line)) { table.push(line.trim()); continue; }
      flushTable();
      if (/^\s*[-*] /.test(line)) { if (!list) { out.push("<ul>"); list = true; } out.push(`<li>${inline(line.replace(/^\s*[-*] /, ""))}</li>`); continue; }
      if (list) { out.push("</ul>"); list = false; }
      const h = line.match(/^(#{1,3}) (.*)/);
      if (h) out.push(`<h3>${inline(h[2])}</h3>`);
      else if (line.trim()) out.push(`<p>${inline(line)}</p>`);
    }
    flushTable();
    if (list) out.push("</ul>");
    return out.join("");
  }

  function viewRecaps() {
    if (!L.recaps.length) return `<h1>Recaps</h1><div class="card empty-state">No recaps yet.</div>`;
    return `<h1>Recaps</h1>${L.recaps.map((r) => `<div class="card md" style="margin-bottom:12px">${md(r.markdown)}</div>`).join("")}`;
  }

  function viewRules() {
    const R = L.rules, s = R.scoring, sched = R.schedule, w = R.waivers, tr = R.trades;
    const n = (v) => (v > 0 ? "+" : "") + (Number.isInteger(v) ? v : Number(v).toString());
    const table = (title, rows) => `<div class="card"><b>${title}</b><table><tbody>${rows.map(([k, v]) =>
      `<tr><td>${k}</td><td class="num">${v}</td></tr>`).join("")}</tbody></table></div>`;
    const starters = R.roster.starters.map((x) => x === "DST" ? "DEF" : x);
    const allowed = R.dst_allowed.map(([lim, pts], i) => [i === 0 ? "0" : `${R.dst_allowed[i - 1][0] + 1}-${lim}`, n(pts)])
      .concat([[`${R.dst_allowed[R.dst_allowed.length - 1][0] + 1}+`, n(R.dst_allowed_else)]]);
    const day = (spec) => spec.replace(/^(\w{3}) (\d+):(\d+)$/, (_, d, h, m) => `${d} ${((+h + 11) % 12) + 1}:${m} ${+h < 12 ? "am" : "pm"}`);
    return `<h1>Rules</h1>
      <div class="grid">
        ${table("League", [["Format", R.format === "redraft" ? "Redraft" : esc(R.format)], ["Teams", R.teams],
          ["Regular season", `Weeks ${sched.start_week}-${sched.regular_season_end_week}`],
          ["Playoffs", `Top ${sched.playoff_teams}, weeks ${sched.playoff_weeks.join("-")}`],
          ["Semifinals", "1 v 4, 2 v 3"], ["Seeding", "Record, then points for"],
          ["Draft", `Snake, ${R.draft_rounds} rounds`]])}
        ${table("Roster", [["Starters", starters.join(", ")], ["FLEX", "RB / WR / TE"], ["Bench", R.roster.bench],
          ["IR", `${R.roster.ir} (statuses ${R.roster.ir_eligible.join(", ")})`], ["Lineup lock", "Each player at his game's kickoff"]])}
        ${table("Passing", [["Passing yard", n(s.pass_yd) + " (1 per 25)"], ["Passing TD", n(s.pass_td)], ["Interception", n(s.pass_int)]])}
        ${table("Rushing & receiving", [["Rushing yard", n(s.rush_yd) + " (1 per 10)"], ["Rushing TD", n(s.rush_td)],
          ["Reception", n(s.rec)], ["Receiving yard", n(s.rec_yd) + " (1 per 10)"], ["Receiving TD", n(s.rec_td)],
          ["Return or fumble-recovery TD", n(R.other_td)], ["2-point conversion", n(s.two_pt)], ["Fumble lost", n(s.fumble_lost)]])}
        ${table("Kicker", [["FG 0-39 yards", n(R.kicker.fg_0_39)], ["FG 40-49 yards", n(R.kicker.fg_40_49)],
          ["FG 50+ yards", n(R.kicker.fg_50_plus)], ["Extra point", n(R.kicker.xp)], ["Missed FG", n(R.kicker.missed_fg)],
          ["Missed extra point", n(R.kicker.missed_xp)]])}
        ${table("Defense / special teams", [["Sack", n(R.dst.sack)], ["Interception", n(R.dst.int)],
          ["Fumble recovery", n(R.dst.fum_rec)], ["Safety", n(R.dst.safety)], ["Blocked kick", n(R.dst.blocked_kick)],
          ["Defensive or return TD", n(R.dst.td)]])}
        ${table("Points allowed (DEF)", allowed)}
        ${table("Waivers", [["Type", `Blind FAAB, $${w.budget} budget`], ["Minimum bid", `$${w.min_bid}`],
          ["Processed (ET)", w.process.map(day).join(", ")], ["Tiebreak", "Team lower in the standings"],
          ["Free agency", "None; every pickup is a bid"]])}
        ${table("Trades", [["Review", tr.review === "commissioner" ? "Commissioner approval" : "None"],
          ["Auto-approved after", `${tr.auto_approve_hours} hours`], ["Offers expire after", `${tr.expires_hours} hours`],
          ["Open offers per team", tr.max_open_proposals], ["Deadline", `Before week ${tr.deadline_week + 1} kicks off`]])}
      </div>`;
  }

  function viewAgents() {
    const rows = L.teams.map((t) => `<tr><td>${teamTag(t.id)}</td><td class="ink2">${esc(t.harness)}<span class="sub"><code>${esc(t.model)}</code></span></td>
      <td class="num">${t.agent.runs ?? 0}</td><td class="num">${t.agent.failed ?? 0}</td><td class="num">${fmt1(t.agent.avg_min)}</td>
      <td class="num">${t.agent.searches}</td><td class="num">${t.agent.fetches}</td><td class="num">${t.agent.tool_calls}</td></tr>`).join("");
    return `<h1>Agents</h1>
      <div class="card scroll"><table><thead><tr><th>Team</th><th>Harness</th><th class="num">Runs</th><th class="num">Failed</th><th class="num">Avg min</th><th class="num">Searches</th><th class="num">Pages read</th><th class="num">Tool calls</th></tr></thead><tbody>${rows}</tbody></table></div>`;
  }

  // Router & chrome ----------------------------------------------------------------------------
  const routes = [
    [/^$/, () => viewMatchups()], [/^w\/(\d+)$/, (m) => viewMatchups(+m[1])], [/^m\/(\d+)\/(\w+)$/, (m) => viewMatchup(+m[1], m[2])],
    [/^standings$/, viewStandings], [/^teams$/, viewTeams], [/^t\/(\w+)$/, (m) => viewTeam(m[1])], [/^draft$/, viewDraft],
    [/^activity(?:\/(\w+))?(?:\?team=(\w+))?$/, (m) => viewActivity(m[1], m[2])], [/^recaps$/, viewRecaps], [/^agents$/, viewAgents], [/^rules$/, viewRules],
  ];
  function render() {
    const path = location.hash.replace(/^#\/?/, "");
    const hit = routes.find(([re]) => re.test(path)) || routes[0];
    app.innerHTML = hit[1](path.match(hit[0]) || []);
    const section = path.split(/[/?]/)[0];
    const tf = document.getElementById("tf");
    if (tf) tf.addEventListener("change", () => {
      const sub = (path.match(/^activity\/(\w+)/) || [])[1] || "board";
      location.hash = `#/activity/${sub}${tf.value ? "?team=" + tf.value : ""}`;
    });
    const tabFor = { "": "", w: "", m: "", t: "teams" }[section] ?? section;
    document.querySelectorAll("#tabs a").forEach((a) => {
      if (a.getAttribute("href") === "#/" + tabFor) a.setAttribute("aria-current", "page");
      else a.removeAttribute("aria-current");
    });
    document.getElementById("brand-sub").textContent = drafted() ? `Season ${L.league.season} · Week ${L.league.current_week}` : `Season ${L.league.season} · Draft`;
    document.getElementById("foot").innerHTML = `Updated ${esc(fmtWhen(L.league.updated))} ET`;
    bindTips(app);
  }

  // Theme toggle (per viewer; storage may be unavailable)
  const themeBtn = document.getElementById("theme");
  const getTheme = () => { try { return localStorage.getItem("theme"); } catch { return null; } };
  const setTheme = (t) => { document.documentElement.dataset.theme = t; try { localStorage.setItem("theme", t); } catch {} label(); };
  const current = () => document.documentElement.dataset.theme || (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
  const label = () => (themeBtn.textContent = current() === "dark" ? "Light mode" : "Dark mode");
  if (getTheme()) document.documentElement.dataset.theme = getTheme();
  themeBtn.addEventListener("click", () => setTheme(current() === "dark" ? "light" : "dark"));
  label();

  // Refresh data every two minutes without a page reload.
  setInterval(() => {
    const s = document.createElement("script");
    s.src = "data.js?t=" + Date.now();
    s.onload = () => {
      if (ASSETS && window.LEAGUE.league.assets && window.LEAGUE.league.assets !== ASSETS) return location.reload();
      L = window.LEAGUE; render(); s.remove();
    };
    document.body.appendChild(s);
  }, 120000);

  addEventListener("hashchange", () => { render(); scrollTo(0, 0); });
  render();
})();
