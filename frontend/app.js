/* Heat Check front end. Reads data/streaks.json and renders the strip, the
 * hot/cold lists and the player detail. Data goes in via textContent only. */
(() => {
  "use strict";

  const DATA_URL = "data/streaks.json";
  const STALE_AFTER_HOURS = 36;
  const LIST_LIMIT = 10;
  const METER_FULL_SCALE = 4; // heat index that fills the little meter
  const SVG_NS = "http://www.w3.org/2000/svg";
  const MINUS = "−";
  const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)");

  const PHRASES = {
    gmsc: { noun: "Game Score", perGame: false },
    pts: { noun: "points", perGame: true },
    reb: { noun: "rebounds", perGame: true },
    ast: { noun: "assists", perGame: true },
    fg3m: { noun: "threes", perGame: true },
  };

  const state = {
    data: null,
    metric: "gmsc",
    defaultMetric: "gmsc",
    selected: null,
    expanded: { hot: false, cold: false },
    byKey: new Map(),
    metrics: new Map(),
  };

  // helpers

  const $ = (id) => document.getElementById(id);

  function el(tag, attrs = {}, ...children) {
    const node = document.createElement(tag);
    for (const [key, value] of Object.entries(attrs)) {
      if (value === null || value === undefined || value === false) continue;
      if (key === "class") node.className = value;
      else node.setAttribute(key, value === true ? "" : String(value));
    }
    for (const child of children.flat()) {
      if (child === null || child === undefined || child === false) continue;
      node.append(child instanceof Node ? child : document.createTextNode(String(child)));
    }
    return node;
  }

  function svgEl(tag, attrs = {}) {
    const node = document.createElementNS(SVG_NS, tag);
    for (const [key, value] of Object.entries(attrs)) {
      if (value !== null && value !== undefined) node.setAttribute(key, String(value));
    }
    return node;
  }

  const oneDecimal = new Intl.NumberFormat("en-US", { minimumFractionDigits: 1, maximumFractionDigits: 1 });
  const num = (n) => (n < 0 ? MINUS : "") + oneDecimal.format(Math.abs(n));
  const signed = (n) => (n > 0 ? "+" : n < 0 ? MINUS : "") + oneDecimal.format(Math.abs(n));
  const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));

  function shortDate(iso) {
    const [y, m, d] = iso.split("-").map(Number);
    return new Date(Date.UTC(y, m - 1, d)).toLocaleDateString("en-US", { month: "short", day: "numeric", timeZone: "UTC" });
  }

  function updatedLabel(date) {
    const text = date.toLocaleString("en-US", {
      month: "short",
      day: "numeric",
      hour: "numeric",
      minute: "2-digit",
      timeZone: "America/New_York",
    });
    return `${text} ET`;
  }

  function normalizeName(text) {
    return text
      .normalize("NFKD")
      .replace(/[̀-ͯ]/g, "")
      .toLowerCase()
      .replace(/[^a-z0-9 ]+/g, "")
      .replace(/\s+/g, " ")
      .trim();
  }

  const params = () => state.data.params;
  const phrase = (m) => PHRASES[m] || { noun: state.metrics.get(m).label.toLowerCase(), perGame: true };
  const statusOf = (p, m) => p.metrics[m].status;
  const teamName = (code) => (state.data.teams && state.data.teams[code]) || code;

  function summaryFor(p, m) {
    const s = p.metrics[m];
    const { noun, perGame } = phrase(m);
    const dir = s.delta >= 0 ? "up" : "down";
    return [
      el("b", {}, num(s.recent)),
      ` ${noun}${perGame ? " a game" : ""} over his last ${params().window}, ${dir} from `,
      el("b", {}, num(s.baseline)),
      ".",
    ];
  }

  function notesFor(p, m) {
    const s = p.metrics[m];
    const notes = [];
    if (Math.abs(s.run) >= 3) {
      notes.push(`${Math.abs(s.run)} straight games ${s.run > 0 ? "above" : "below"} his usual.`);
    }
    if (p.role_change) {
      const dir = p.min_recent > p.min_baseline ? "up" : "down";
      notes.push(`Minutes ${dir} from ${Math.round(p.min_baseline)} to ${Math.round(p.min_recent)}.`);
    }
    return notes.join(" ");
  }

  // tooltip

  const tooltip = $("tooltip");

  function showTooltip(clientX, clientY, value, lines) {
    tooltip.replaceChildren(
      el("span", { class: "tooltip__value" }, value),
      ...lines.map((line) => el("span", { class: "tooltip__line" }, line)),
    );
    tooltip.hidden = false;
    const box = tooltip.getBoundingClientRect();
    let left = clientX + 14;
    let top = clientY + 16;
    if (left + box.width > window.innerWidth - 8) left = clientX - box.width - 14;
    if (top + box.height > window.innerHeight - 8) top = clientY - box.height - 12;
    tooltip.style.left = `${Math.max(8, left)}px`;
    tooltip.style.top = `${Math.max(8, top)}px`;
  }

  function hideTooltip() {
    tooltip.hidden = true;
  }

  function gameLine(g) {
    return `${shortDate(g.date)} ${g.home ? "vs" : "at"} ${g.opp}, ${g.min} min`;
  }

  /** Crosshair + tooltip for a small line or column chart. */
  function attachSeriesHover(svg, points, width, height, describe) {
    const hair = svgEl("line", { class: "crosshair", y1: 0, y2: height, visibility: "hidden" });
    svg.append(hair);
    svg.addEventListener("pointermove", (ev) => {
      const rect = svg.getBoundingClientRect();
      const px = (ev.clientX - rect.left) * (width / rect.width);
      let best = points[0];
      for (const pt of points) if (Math.abs(pt.x - px) < Math.abs(best.x - px)) best = pt;
      hair.setAttribute("x1", best.x);
      hair.setAttribute("x2", best.x);
      hair.setAttribute("visibility", "visible");
      const [value, ...lines] = describe(best);
      showTooltip(ev.clientX, ev.clientY, value, lines);
    });
    svg.addEventListener("pointerleave", () => {
      hair.setAttribute("visibility", "hidden");
      hideTooltip();
    });
  }

  // header + notices

  function renderFreshness() {
    const data = state.data;
    const updated = new Date(data.generated_at);
    $("freshness").textContent = `Updated ${updatedLabel(updated)}, through games of ${shortDate(data.as_of)}`;

    const notice = $("notice");
    notice.replaceChildren();
    const hoursOld = (Date.now() - updated.getTime()) / 36e5;
    if (data.sample) {
      notice.append(
        el("strong", {}, "Sample data. "),
        "These players and numbers are made up so you can preview the site. The real league appears after the first nightly update.",
      );
    } else if (hoursOld > STALE_AFTER_HOURS) {
      notice.append(
        el("strong", {}, "Out of date. "),
        `The last update ran ${updatedLabel(updated)}, so games since ${shortDate(data.as_of)} are missing.`,
      );
    }
    notice.hidden = notice.childNodes.length === 0;
  }

  function fillMethodParams() {
    const p = params();
    const values = { ...p, min_games: p.window + p.baseline_min };
    for (const node of document.querySelectorAll("[data-param]")) {
      const value = values[node.dataset.param];
      if (value !== undefined) node.textContent = String(value);
    }
  }

  // stat switch

  function renderSwitch() {
    const box = $("stat-options");
    box.replaceChildren();
    for (const metric of state.data.metrics) {
      const id = `stat-${metric.key}`;
      const input = el("input", { type: "radio", name: "stat", id, value: metric.key });
      input.checked = metric.key === state.metric;
      input.addEventListener("change", () => setMetric(metric.key));
      box.append(input, el("label", { for: id }, metric.label));
    }
  }

  function setMetric(key) {
    if (!state.metrics.has(key)) return;
    state.metric = key;
    const input = $(`stat-${key}`);
    if (input) input.checked = true;
    writeHash();
    renderStrip();
    renderLists();
    renderDetail();
  }

  // the strip

  const strip = { svg: null, layers: null, dots: new Map(), positions: [], size: { width: 0, height: 0 } };

  /** Place dots at their x and the smallest |y| that does not overlap a neighbour. */
  function beeswarm(items, gap) {
    const sorted = [...items].sort((a, b) => a.x - b.x);
    const placed = [];
    for (const item of sorted) {
      const near = [];
      for (let i = placed.length - 1; i >= 0 && item.x - placed[i].x < gap; i--) near.push(placed[i]);
      let y = 0;
      if (near.length) {
        const candidates = [0];
        for (const p of near) {
          const dy = Math.sqrt(gap * gap - (p.x - item.x) ** 2);
          candidates.push(p.y + dy, p.y - dy);
        }
        candidates.sort((a, b) => Math.abs(a) - Math.abs(b));
        y = candidates.find((c) => near.every((p) => (p.x - item.x) ** 2 + (p.y - c) ** 2 >= gap * gap - 0.01));
      }
      placed.push({ ...item, y });
    }
    return placed;
  }

  function ensureStripSvg(host) {
    if (strip.svg && host.contains(strip.svg)) return;
    const svg = svgEl("svg", { role: "img" });
    const layers = {
      zones: svgEl("g"),
      axis: svgEl("g"),
      dots: svgEl("g"),
      labels: svgEl("g"),
    };
    svg.append(layers.zones, layers.axis, layers.dots, layers.labels);
    host.replaceChildren(svg);
    strip.svg = svg;
    strip.layers = layers;
    strip.dots.clear();

    const nearest = (ev) => {
      const rect = svg.getBoundingClientRect();
      const sx = strip.size.width / rect.width;
      const sy = strip.size.height / rect.height;
      const px = (ev.clientX - rect.left) * sx;
      const py = (ev.clientY - rect.top) * sy;
      let best = null;
      let bestDist = 18 * 18;
      for (const pos of strip.positions) {
        const d = (pos.x - px) ** 2 + (pos.y - py) ** 2;
        if (d < bestDist) {
          best = pos;
          bestDist = d;
        }
      }
      return best;
    };
    svg.addEventListener("pointermove", (ev) => {
      const hit = nearest(ev);
      svg.style.cursor = hit ? "pointer" : "default";
      if (!hit) return hideTooltip();
      const p = state.byKey.get(hit.key);
      const s = p.metrics[state.metric];
      showTooltip(ev.clientX, ev.clientY, `${signed(s.z)} heat index`, [
        `${p.name}, ${p.team}`,
        `${num(s.recent)} over his last ${params().window}, usual ${num(s.baseline)}`,
      ]);
    });
    svg.addEventListener("pointerleave", hideTooltip);
    svg.addEventListener("click", (ev) => {
      const hit = nearest(ev);
      if (hit) selectPlayer(hit.key, { scroll: true });
    });
  }

  function renderStrip() {
    const host = $("thermo-chart");
    const players = state.data.players;
    const m = state.metric;
    const { noun } = phrase(m);

    if (!players.length) {
      strip.svg = null;
      $("thermo-caption").textContent = "";
      host.replaceChildren(
        el(
          "p",
          { class: "empty" },
          `No players are ranked yet. Each player needs ${params().window + params().baseline_min} games of at least ${params().min_minutes} minutes, so the rankings fill in as games are played.`,
        ),
      );
      return;
    }

    $("thermo-caption").textContent =
      `Each dot is one of ${players.length} ranked players, placed by how far his last ${params().window} games ` +
      `are from his usual ${noun}. Dots in the shaded ends count as heating up or cooling off.`;

    ensureStripSvg(host);
    const { svg, layers } = strip;
    const width = Math.max(host.clientWidth, 280);
    const narrow = width < 560;
    const r = narrow ? 4 : 4.5;
    const margin = { top: 46, right: 10, bottom: 44, left: 10 };
    const zs = players.map((p) => p.metrics[m].z);
    const limit = clamp(Math.ceil(Math.max(...zs.map(Math.abs)) + 0.2), 3, 6);
    const innerW = width - margin.left - margin.right;
    const x = (z) => margin.left + ((clamp(z, -limit, limit) + limit) / (2 * limit)) * innerW;

    const placed = beeswarm(
      players.map((p) => ({ key: p.key, x: x(p.metrics[m].z), z: p.metrics[m].z, status: statusOf(p, m) })),
      r * 2 + 1.5,
    );
    const maxSpread = narrow ? 60 : 80;
    const spread = Math.max(r * 2, ...placed.map((d) => Math.abs(d.y)));
    const squeeze = spread > maxSpread ? maxSpread / spread : 1;
    const half = Math.min(spread, maxSpread) + r + 4;
    const centerY = margin.top + half;
    const height = centerY + half + margin.bottom;
    strip.size = { width, height };
    svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
    svg.setAttribute("width", width);
    svg.setAttribute("height", height);

    // Zones and axis
    const threshold = params().threshold;
    const top = centerY - half;
    const bottom = centerY + half;
    layers.zones.replaceChildren(
      svgEl("rect", { class: "zone zone--cold", x: margin.left, y: top, width: x(-threshold) - margin.left, height: half * 2 }),
      svgEl("rect", { class: "zone zone--hot", x: x(threshold), y: top, width: width - margin.right - x(threshold), height: half * 2 }),
      svgEl("line", { class: "axis-zero", x1: x(0), x2: x(0), y1: top, y2: bottom }),
      svgEl("line", { class: "axis-line", x1: margin.left, x2: width - margin.right, y1: bottom, y2: bottom }),
    );
    const step = narrow ? 2 : 1;
    const axis = [];
    for (let t = -limit; t <= limit; t += step) {
      axis.push(svgEl("line", { class: "tick", x1: x(t), x2: x(t), y1: bottom, y2: bottom + 4 }));
      const label = svgEl("text", { class: "tick-label", x: x(t), y: bottom + 17, "text-anchor": "middle" });
      label.textContent = t === 0 ? "0" : signed(t).replace(".0", "");
      axis.push(label);
    }
    const poles = [
      ["Colder than usual", margin.left, "start"],
      ["Hotter than usual", width - margin.right, "end"],
    ];
    for (const [text, px, anchor] of poles) {
      const label = svgEl("text", { class: "axis-label", x: px, y: bottom + 36, "text-anchor": anchor });
      label.textContent = text;
      axis.push(label);
    }
    layers.axis.replaceChildren(...axis);

    // Dots, keyed by player so switching stats animates them to their new spot
    const seen = new Set();
    strip.positions = [];
    const firstDraw = strip.dots.size === 0;
    for (const d of placed) {
      seen.add(d.key);
      const cy = centerY + d.y * squeeze;
      strip.positions.push({ key: d.key, x: d.x, y: cy });
      let dot = strip.dots.get(d.key);
      if (!dot) {
        dot = svgEl("circle", { cx: 0, cy: 0 });
        dot.style.transform = `translate(${d.x}px, ${centerY}px)`;
        if (firstDraw && !reduceMotion.matches) dot.style.opacity = "0";
        layers.dots.append(dot);
        strip.dots.set(d.key, dot);
      }
      dot.setAttribute("r", r);
      dot.setAttribute("class", `dot dot--${d.status}`);
      const target = `translate(${d.x}px, ${cy}px)`;
      if (firstDraw && !reduceMotion.matches) {
        requestAnimationFrame(() =>
          requestAnimationFrame(() => {
            dot.style.transform = target;
            dot.style.opacity = "1";
          }),
        );
      } else {
        dot.style.transform = target;
      }
    }
    for (const [key, dot] of strip.dots) {
      if (!seen.has(key)) {
        dot.remove();
        strip.dots.delete(key);
      }
    }

    // Labels: the hottest and coldest player, plus whoever is selected. Two
    // label rows sit above the swarm; a label that fits in neither is skipped
    // (the tooltip, the lists and the detail panel still carry it).
    const labels = [];
    const byZ = [...strip.positions].sort((a, b) => state.byKey.get(b.key).metrics[m].z - state.byKey.get(a.key).metrics[m].z);
    const hottest = byZ[0];
    const coldest = byZ[byZ.length - 1];
    const rows = [top - 10, top - 27];
    const taken = [];
    const addLabel = (pos) => {
      const p = state.byKey.get(pos.key);
      const zText = signed(p.metrics[m].z);
      const estimate = (p.name.length + zText.length + 1) * 6.9 + 4;
      const anchor = pos.x > width * 0.72 ? "end" : pos.x < width * 0.28 ? "start" : "middle";
      const x0 = anchor === "end" ? pos.x - estimate : anchor === "start" ? pos.x : pos.x - estimate / 2;
      const x1 = x0 + estimate;
      const row = rows.findIndex((_, i) => !taken.some((t) => t.row === i && x1 > t.x0 - 10 && x0 < t.x1 + 10));
      if (row === -1) return;
      taken.push({ row, x0, x1 });
      const y = rows[row];
      const text = svgEl("text", { class: "extreme-label", x: pos.x, y, "text-anchor": anchor });
      const name = svgEl("tspan");
      name.textContent = `${p.name} `;
      const z = svgEl("tspan", { class: "z" });
      z.textContent = zText;
      text.append(name, z);
      labels.push(svgEl("line", { class: "leader", x1: pos.x, x2: pos.x, y1: y + 4, y2: pos.y - r - 2 }), text);
    };
    if (hottest && statusOf(state.byKey.get(hottest.key), m) === "hot") addLabel(hottest);
    if (coldest && coldest !== hottest && statusOf(state.byKey.get(coldest.key), m) === "cold") addLabel(coldest);
    const selected = strip.positions.find((pos) => pos.key === state.selected);
    if (selected) {
      if (selected !== hottest && selected !== coldest) addLabel(selected);
      labels.push(svgEl("circle", { class: "selected-ring", cx: selected.x, cy: selected.y, r: r + 4 }));
    }
    layers.labels.replaceChildren(...labels);

    const hotP = hottest && state.byKey.get(hottest.key);
    const coldP = coldest && state.byKey.get(coldest.key);
    svg.setAttribute(
      "aria-label",
      `Heat index by ${noun} for ${players.length} players. Hottest: ${hotP.name}, ${signed(hotP.metrics[m].z)}. ` +
        `Coldest: ${coldP.name}, ${signed(coldP.metrics[m].z)}. The lists below have the details.`,
    );
  }

  // lists

  function sparkline(p, m, kind) {
    const W = 112;
    const H = 36;
    const pad = 5;
    const history = p.history;
    const values = history.map((g) => g[m]);
    const base = p.metrics[m].baseline;
    const lo = Math.min(base, ...values);
    const hi = Math.max(base, ...values);
    const span = hi - lo || 1;
    const x = (i) => pad + (i * (W - 2 * pad)) / Math.max(history.length - 1, 1);
    const y = (v) => H - pad - ((v - lo) / span) * (H - 2 * pad);
    const path = (from, to) =>
      values
        .slice(from, to + 1)
        .map((v, j) => `${j ? "L" : "M"}${x(from + j).toFixed(1)},${y(v).toFixed(1)}`)
        .join("");

    const svg = svgEl("svg", { class: "spark", viewBox: `0 0 ${W} ${H}`, width: W, height: H, "aria-hidden": "true" });
    svg.append(svgEl("line", { class: "spark__base", x1: pad, x2: W - pad, y1: y(base), y2: y(base) }));
    const firstRecent = Math.max(0, history.findIndex((g) => g.recent));
    if (firstRecent > 0) svg.append(svgEl("path", { class: "spark__line", d: path(0, firstRecent) }));
    svg.append(svgEl("path", { class: `spark__line spark__line--${kind}`, d: path(firstRecent, values.length - 1) }));
    const last = values.length - 1;
    svg.append(svgEl("circle", { class: `spark__dot spark__dot--${kind}`, cx: x(last), cy: y(values[last]), r: 4 }));

    const { noun } = phrase(m);
    attachSeriesHover(
      svg,
      history.map((g, i) => ({ x: x(i), g })),
      W,
      H,
      ({ g }) => [`${num(g[m])} ${noun}`, gameLine(g)],
    );
    return svg;
  }

  function meter(z, kind) {
    const share = clamp(Math.abs(z) / METER_FULL_SCALE, 0.04, 1);
    const fill = el("span", { class: "meter__fill" });
    fill.style.width = `${(share * 100).toFixed(0)}%`;
    return el("span", { class: `meter meter--${kind}`, "aria-hidden": "true" }, fill);
  }

  function rowFor(p, rank, kind) {
    const m = state.metric;
    const s = p.metrics[m];
    const button = el(
      "button",
      {
        class: "row",
        type: "button",
        "aria-current": p.key === state.selected ? "true" : null,
      },
      el("span", { class: "row__rank" }, String(rank)),
      el("span", { class: "row__name" }, el("span", { class: "row__player" }, p.name), el("span", { class: "row__team" }, p.team)),
      el("span", { class: "row__summary" }, summaryFor(p, m)),
      el("span", { class: "row__spark" }, sparkline(p, m, kind)),
      el(
        "span",
        { class: "row__heat" },
        el(
          "span",
          { class: "heat" },
          el("span", { class: "visually-hidden" }, "Heat index "),
          el("span", { class: "heat__value" }, signed(s.z)),
          meter(s.z, kind),
        ),
      ),
      el("span", { class: "row__notes" }, notesFor(p, m)),
    );
    button.addEventListener("click", () => selectPlayer(p.key, { scroll: true }));
    return button;
  }

  function renderList(kind, players) {
    const list = $(`${kind}-list`);
    const more = $(`${kind}-more`);
    $(`${kind}-count`).textContent = players.length === 1 ? "1 player" : `${players.length} players`;

    const head = list.previousElementSibling;
    if (!head || !head.classList.contains("list__head")) {
      list.before(
        el(
          "div",
          { class: "list__head", "aria-hidden": "true" },
          el("span", { class: "list__head-spark" }, `Last ${params().history_games} games`),
          el("span", { class: "list__head-heat" }, "Heat index"),
        ),
      );
    }

    list.replaceChildren();
    const { noun } = phrase(state.metric);
    if (!players.length) {
      list.append(
        el(
          "li",
          { class: "list__empty" },
          kind === "hot" ? `Nobody is running hot by ${noun} right now.` : `Nobody is in a slump by ${noun} right now.`,
        ),
      );
      more.hidden = true;
      return;
    }
    const shown = state.expanded[kind] ? players : players.slice(0, LIST_LIMIT);
    shown.forEach((p, i) => list.append(el("li", {}, rowFor(p, i + 1, kind))));
    more.hidden = players.length <= LIST_LIMIT;
    more.textContent = state.expanded[kind] ? `Show the top ${LIST_LIMIT}` : `Show all ${players.length}`;
    more.onclick = () => {
      state.expanded[kind] = !state.expanded[kind];
      renderList(kind, players);
    };
  }

  function renderLists() {
    const m = state.metric;
    const ranked = (status, direction) =>
      state.data.players
        .filter((p) => statusOf(p, m) === status)
        .sort((a, b) => direction * (b.metrics[m].z - a.metrics[m].z));
    renderList("hot", ranked("hot", 1));
    renderList("cold", ranked("cold", -1));
  }

  // player detail

  function niceTicks(lo, hi, count) {
    const raw = (hi - lo) / count || 1;
    const mag = 10 ** Math.floor(Math.log10(raw));
    const norm = raw / mag;
    const step = (norm < 1.5 ? 1 : norm < 3 ? 2 : norm < 7 ? 5 : 10) * mag;
    const ticks = [];
    for (let t = Math.ceil(lo / step) * step; t <= hi + 1e-9; t += step) ticks.push(Math.round(t * 100) / 100);
    return ticks;
  }

  function barPath(x, w, y0, y1, radius) {
    const h = Math.abs(y1 - y0);
    if (h < 0.5) return "";
    const r = Math.min(radius, h, w / 2);
    if (y1 < y0) {
      return `M${x},${y0}V${y1 + r}A${r},${r} 0 0 1 ${x + r},${y1}H${x + w - r}A${r},${r} 0 0 1 ${x + w},${y1 + r}V${y0}Z`;
    }
    return `M${x},${y0}V${y1 - r}A${r},${r} 0 0 0 ${x + r},${y1}H${x + w - r}A${r},${r} 0 0 0 ${x + w},${y1 - r}V${y0}Z`;
  }

  function columnChart(p, m, kind) {
    const W = 560;
    const H = 230;
    const margin = { top: 30, right: 70, bottom: 34, left: 34 };
    const history = p.history;
    const values = history.map((g) => g[m]);
    const base = p.metrics[m].baseline;
    const lo = Math.min(0, ...values);
    const hi = Math.max(base, ...values, 1) * 1.08;
    const innerW = W - margin.left - margin.right;
    const innerH = H - margin.top - margin.bottom;
    const y = (v) => margin.top + ((hi - v) / (hi - lo)) * innerH;
    const slot = innerW / history.length;
    const barW = Math.min(24, slot - 4);
    const slotX = (i) => margin.left + i * slot;
    const { noun } = phrase(m);

    const svg = svgEl("svg", {
      viewBox: `0 0 ${W} ${H}`,
      role: "img",
      "aria-label": `${state.metrics.get(m).label} in each of ${p.name}'s last ${history.length} games, with his usual level marked. The game log table has every value.`,
    });

    const firstRecent = Math.max(0, history.findIndex((g) => g.recent));
    svg.append(
      svgEl("rect", {
        class: "window-wash",
        x: slotX(firstRecent),
        y: margin.top - 22,
        width: slot * (history.length - firstRecent),
        height: innerH + 22,
      }),
    );
    const windowLabel = svgEl("text", { class: "axis-label", x: slotX(firstRecent) + 6, y: margin.top - 8 });
    windowLabel.textContent = `Last ${history.length - firstRecent} games`;
    svg.append(windowLabel);

    for (const t of niceTicks(lo, hi, 4)) {
      svg.append(svgEl("line", { class: t === 0 ? "axis-line" : "grid", x1: margin.left, x2: margin.left + innerW, y1: y(t), y2: y(t) }));
      const label = svgEl("text", { class: "tick-label", x: margin.left - 8, y: y(t) + 4, "text-anchor": "end" });
      label.textContent = t < 0 ? `${MINUS}${Math.abs(t)}` : String(t);
      svg.append(label);
    }

    const bars = [];
    history.forEach((g, i) => {
      const cls = g.recent ? `bar bar--${kind}` : "bar bar--past";
      const bx = slotX(i) + (slot - barW) / 2;
      const d = barPath(bx, barW, y(0), y(g[m]), 4);
      if (d) {
        const bar = svgEl("path", { class: cls, d });
        svg.append(bar);
        bars.push(bar);
      } else {
        bars.push(null);
      }
    });

    svg.append(svgEl("line", { class: "usual-line", x1: margin.left, x2: margin.left + innerW, y1: y(base), y2: y(base) }));
    const usual = svgEl("text", { class: "usual-label", x: margin.left + innerW + 8, y: y(base) - 2 });
    const usualWord = svgEl("tspan", { x: margin.left + innerW + 8, dy: 0 });
    usualWord.textContent = "Usual";
    const usualValue = svgEl("tspan", { x: margin.left + innerW + 8, dy: 15, class: "usual-value" });
    usualValue.textContent = num(base);
    usual.append(usualWord, usualValue);
    svg.append(usual);

    const dateIdx = [...new Set([0, firstRecent, history.length - 1])];
    for (const i of dateIdx) {
      const label = svgEl("text", { class: "tick-label", x: slotX(i) + slot / 2, y: H - 12, "text-anchor": "middle" });
      label.textContent = shortDate(history[i].date);
      svg.append(label);
    }

    attachSeriesHover(
      svg,
      history.map((g, i) => ({ x: slotX(i) + slot / 2, g, i })),
      W,
      H,
      ({ g, i }) => {
        bars.forEach((bar, j) => bar && bar.classList.toggle("is-dim", j !== i));
        return [`${num(g[m])} ${noun}`, gameLine(g)];
      },
    );
    svg.addEventListener("pointerleave", () => bars.forEach((bar) => bar && bar.classList.remove("is-dim")));
    return svg;
  }

  function statsTable(p) {
    const head = el(
      "thead",
      {},
      el("tr", {}, ["Stat", `Last ${params().window}`, "Usual", "Change", "Heat index"].map((t) => el("th", { scope: "col" }, t))),
    );
    const rows = state.data.metrics.map((meta) => {
      const s = p.metrics[meta.key];
      const label = s.status === "hot" ? "Heating up" : s.status === "cold" ? "Cooling off" : "Near usual";
      return el(
        "tr",
        { class: meta.key === state.metric ? "is-selected-metric" : null },
        el("th", { scope: "row" }, meta.label),
        el("td", {}, num(s.recent)),
        el("td", {}, num(s.baseline)),
        el("td", {}, signed(s.delta)),
        el("td", {}, el("span", { class: `chip chip--${s.status}`, title: label }, signed(s.z)), el("span", { class: "visually-hidden" }, ` (${label})`)),
      );
    });
    rows.push(
      el(
        "tr",
        {},
        el("th", { scope: "row" }, "Minutes"),
        el("td", {}, num(p.min_recent)),
        el("td", {}, num(p.min_baseline)),
        el("td", {}, signed(p.min_recent - p.min_baseline)),
        el("td", {}, ""),
      ),
    );
    return el(
      "div",
      { class: "table-wrap" },
      el("table", { class: "data-table" }, el("caption", {}, `His last ${params().window} games against his usual level`), head, el("tbody", {}, rows)),
    );
  }

  function gameLogTable(p) {
    const cols = [
      ["Date", (g) => shortDate(g.date)],
      ["Opponent", (g) => `${g.home ? "vs" : "at"} ${g.opp}`],
      ["Min", (g) => String(g.min)],
      ["PTS", (g) => String(Math.round(g.pts))],
      ["REB", (g) => String(Math.round(g.reb))],
      ["AST", (g) => String(Math.round(g.ast))],
      ["3PM", (g) => String(Math.round(g.fg3m))],
      ["GmSc", (g) => num(g.gmsc)],
    ];
    const head = el("thead", {}, el("tr", {}, cols.map(([t]) => el("th", { scope: "col" }, t))));
    const body = el(
      "tbody",
      {},
      [...p.history].reverse().map((g) => el("tr", { class: g.recent ? "is-recent" : null }, cols.map(([, f]) => el("td", {}, f(g))))),
    );
    return el(
      "div",
      { class: "table-wrap" },
      el(
        "table",
        { class: "data-table" },
        el("caption", {}, `Game log, newest first. The marked rows are his last ${params().window} games.`),
        head,
        body,
      ),
    );
  }

  function renderDetail(message) {
    const box = $("player-detail");
    if (message) {
      box.replaceChildren(el("p", { class: "detail__message" }, message));
      return;
    }
    const p = state.selected && state.byKey.get(state.selected);
    if (!p) {
      box.replaceChildren(
        el("p", { class: "detail__message" }, "Pick a player in the lists or the chart above, or search for anyone who is ranked."),
      );
      return;
    }
    const m = state.metric;
    const s = p.metrics[m];
    const kind = s.status;
    const { noun } = phrase(m);
    const verdict =
      kind === "hot"
        ? `Heating up by ${noun}, with a heat index of ${signed(s.z)}: `
        : kind === "cold"
          ? `Cooling off by ${noun}, with a heat index of ${signed(s.z)}: `
          : `Close to his usual ${noun} (heat index ${signed(s.z)}): `;

    box.replaceChildren(
      el(
        "div",
        { class: "detail__head" },
        el("h3", { class: "detail__name", id: "detail-name", tabindex: "-1" }, p.name),
        el("span", { class: "detail__team" }, `${teamName(p.team)}, last played ${shortDate(p.last_game)}`),
      ),
      el("p", { class: "detail__verdict" }, verdict, summaryFor(p, m), notesFor(p, m) ? ` ${notesFor(p, m)}` : ""),
      el(
        "div",
        { class: "detail__grid" },
        el("div", { class: "columns" }, el("p", { class: "chart-title" }, `${state.metrics.get(m).label} by game`), columnChart(p, m, kind)),
        statsTable(p),
      ),
      gameLogTable(p),
    );
  }

  function findPlayer(query) {
    const q = normalizeName(query);
    if (!q) return null;
    const players = [...state.data.players].sort((a, b) => a.name.localeCompare(b.name));
    const name = (p) => normalizeName(p.name);
    return (
      players.find((p) => name(p) === q) ||
      players.find((p) => name(p).split(" ").some((part) => part.startsWith(q)) || name(p).startsWith(q)) ||
      players.find((p) => name(p).includes(q)) ||
      null
    );
  }

  function selectPlayer(key, { scroll = false } = {}) {
    state.selected = key;
    const p = state.byKey.get(key);
    if (p) $("player-search").value = p.name;
    writeHash();
    renderDetail();
    renderStrip();
    renderLists();
    if (scroll) {
      // The row or dot that was clicked was just re-rendered, so move focus to the
      // panel that opened instead of losing it to <body>.
      const heading = $("detail-name");
      if (heading) heading.focus({ preventScroll: true });
      $("lookup").scrollIntoView({ behavior: reduceMotion.matches ? "auto" : "smooth", block: "start" });
    }
  }

  function setupLookup() {
    const input = $("player-search");
    const options = $("player-names");
    options.replaceChildren(
      ...[...state.data.players].sort((a, b) => a.name.localeCompare(b.name)).map((p) => el("option", { value: p.name })),
    );
    input.addEventListener("input", () => {
      const q = normalizeName(input.value);
      const exact = state.data.players.find((p) => normalizeName(p.name) === q);
      if (exact && exact.key !== state.selected) selectPlayer(exact.key);
    });
    $("lookup-form").addEventListener("submit", (ev) => {
      ev.preventDefault();
      const match = findPlayer(input.value);
      if (match) {
        selectPlayer(match.key);
      } else if (input.value.trim()) {
        state.selected = null;
        writeHash();
        renderDetail(
          `No ranked player matches "${input.value.trim()}". Players need ${params().window + params().baseline_min} games of at least ${params().min_minutes} minutes, including one in the last ${params().max_days_inactive} days.`,
        );
      }
    });
  }

  // URL state

  function readHash() {
    const hash = new URLSearchParams(window.location.hash.slice(1));
    const stat = hash.get("stat");
    const player = hash.get("player");
    if (stat && state.metrics.has(stat)) state.metric = stat;
    if (player && state.byKey.has(player)) state.selected = player;
  }

  function writeHash() {
    const hash = new URLSearchParams();
    if (state.metric !== state.defaultMetric) hash.set("stat", state.metric);
    if (state.selected) hash.set("player", state.selected);
    const text = hash.toString();
    try {
      history.replaceState(null, "", text ? `#${text}` : window.location.pathname + window.location.search);
    } catch {
      // Some embedded or sandboxed frames refuse history changes; the page works without them.
    }
  }

  // boot

  function init(data) {
    state.data = data;
    state.byKey = new Map(data.players.map((p) => [p.key, p]));
    state.metrics = new Map(data.metrics.map((m) => [m.key, m]));
    state.defaultMetric = data.metrics[0].key;
    state.metric = state.defaultMetric;
    readHash();

    renderFreshness();
    fillMethodParams();
    renderSwitch();
    setupLookup();
    renderStrip();
    renderLists();
    renderDetail();

    let resizeTimer = 0;
    let lastWidth = $("thermo-chart").clientWidth;
    new ResizeObserver(() => {
      const width = $("thermo-chart").clientWidth;
      if (Math.abs(width - lastWidth) < 2) return;
      lastWidth = width;
      document.body.classList.add("is-resizing");
      clearTimeout(resizeTimer);
      resizeTimer = setTimeout(() => {
        renderStrip();
        document.body.classList.remove("is-resizing");
      }, 120);
    }).observe($("thermo-chart"));
  }

  function showError(error) {
    $("freshness").textContent = "Couldn't load the latest numbers";
    const notice = $("notice");
    notice.replaceChildren(el("strong", {}, "No data. "), error.message);
    notice.classList.add("notice--error");
    notice.hidden = false;
    $("thermo-chart").replaceChildren();
  }

  async function load() {
    let response;
    try {
      response = await fetch(DATA_URL, { cache: "no-cache" });
    } catch {
      return showError(new Error(`Couldn't reach ${DATA_URL}. Check your connection and reload.`));
    }
    if (!response.ok) {
      return showError(
        new Error(`Couldn't load ${DATA_URL} (HTTP ${response.status}). The nightly update creates this file, so it may not have run yet.`),
      );
    }
    let data;
    try {
      data = await response.json();
    } catch {
      return showError(new Error(`${DATA_URL} isn't valid JSON.`));
    }
    if (data.schema_version !== 1 || !Array.isArray(data.players)) {
      return showError(new Error(`${DATA_URL} uses a format this page doesn't understand (schema ${data.schema_version}).`));
    }
    init(data);
  }

  load();
})();
