// sitrep dashboard: fetches /api/* and draws the page. No libraries, nothing from the internet.
"use strict";

const STATE_EVERY_MS = 15_000;
const SLOW_EVERY_MS = 60_000;
const TICK_EVERY_MS = 5_000;
const SIMULATION_EVERY_MS = 3_000; // a simulation moves fast, so keep up with it
const STRIP_HOURS = 24;
const STRIP_BUCKET_MINUTES = 30;
const CHART_MAX = 1.1; // network charts run from 0 to 110% of usual

const LEVELS = {
  normal: { label: "Normal", icon: "check" },
  elevated: { label: "Elevated", icon: "alert" },
  critical: { label: "Critical", icon: "critical" },
  offline: { label: "Offline", icon: "offline" },
  learning: { label: "Learning", icon: "learning" },
  failing: { label: "Fetch failing", icon: "clock" },
  frozen: { label: "Frozen", icon: "pause" },
  watch: { label: "Dip", icon: "dip" },
  info: { label: "Info", icon: "dots" },
  stale: { label: "Not checked lately", icon: "clock" },
  unknown: { label: "Starting", icon: "dots" },
};

const OVERALL = {
  unknown: { label: "Starting up", icon: "dots" },
  normal: { label: "All clear", icon: "check" },
  elevated: { label: "Elevated", icon: "alert" },
  critical: { label: "Critical", icon: "critical" },
  offline: { label: "Can't reach the internet", icon: "offline" },
};

const FAVICON_COLOURS = {
  unknown: "#898781", normal: "#0ca30c", elevated: "#fab219", critical: "#d03b3b", offline: "#ec835a",
};

const CATEGORY_LABELS = {
  connectivity: "Connectivity", network: "Network measurement", official: "Official", cyber: "Cyber intel",
  news: "News", social: "Social", stress: "Stress", sensors: "Sensors", info: "Info",
};
const CATEGORY_ORDER = Object.keys(CATEGORY_LABELS);

const CHECK_KINDS = { router: "Router", tcp: "Network", dns: "DNS", https: "Website" };
const CHECK_ORDER = Object.keys(CHECK_KINDS);

// Icons are fixed strings written here, never data from a source.
const ICON_PATHS = {
  check:
    '<circle cx="8" cy="8" r="7.2" fill="currentColor"/>' +
    '<path d="M4.7 8.3l2.2 2.2 4.4-4.8" fill="none" stroke="#fff" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/>',
  alert:
    '<path d="M8 1.3l6.9 12.4H1.1z" fill="currentColor" stroke="currentColor" stroke-width="1.2" stroke-linejoin="round"/>' +
    '<path d="M8 5.9v3.7" stroke="#0b0b0b" stroke-width="1.8" stroke-linecap="round"/>' +
    '<circle cx="8" cy="11.9" r="1" fill="#0b0b0b"/>',
  critical:
    '<path d="M5.1 0.9h5.8L15.1 5.1v5.8L10.9 15.1H5.1L0.9 10.9V5.1z" fill="currentColor"/>' +
    '<path d="M5.6 5.6l4.8 4.8M10.4 5.6l-4.8 4.8" stroke="#fff" stroke-width="1.8" stroke-linecap="round"/>',
  offline:
    '<circle cx="8" cy="8" r="7.2" fill="currentColor"/>' +
    '<path d="M3.9 6.7a6 6 0 0 1 8.2 0M5.6 8.7a3.4 3.4 0 0 1 4.8 0" fill="none" stroke="#fff" stroke-width="1.5" stroke-linecap="round"/>' +
    '<circle cx="8" cy="11" r="1" fill="#fff"/>' +
    '<path d="M3.6 3.6l8.8 8.8" stroke="#fff" stroke-width="1.5" stroke-linecap="round"/>',
  clock:
    '<circle cx="8" cy="8" r="6.3" fill="none" stroke="currentColor" stroke-width="1.6"/>' +
    '<path d="M8 4.6V8l2.3 1.5" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/>',
  dots:
    '<circle cx="3.5" cy="8" r="1.5" fill="currentColor"/><circle cx="8" cy="8" r="1.5" fill="currentColor"/>' +
    '<circle cx="12.5" cy="8" r="1.5" fill="currentColor"/>',
  learning:
    '<circle cx="8" cy="8" r="6.3" fill="none" stroke="currentColor" stroke-width="1.6"/>' +
    '<path d="M8 1.7A6.3 6.3 0 0 1 14.3 8H8z" fill="currentColor"/>',
  pause:
    '<circle cx="8" cy="8" r="6.3" fill="none" stroke="currentColor" stroke-width="1.6"/>' +
    '<path d="M6.4 5.4v5.2M9.6 5.4v5.2" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/>',
  dip:
    '<circle cx="8" cy="8" r="6.3" fill="none" stroke="currentColor" stroke-width="1.6"/>' +
    '<path d="M8 4.5v6.2M5.4 8.3L8 10.9l2.6-2.6" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/>',
};

const app = {
  state: null,
  uptime: null,
  timeline: null,
  skew: 0, // server clock minus this device's clock, in seconds
  lastContact: null,
  serverDown: false,
  openDetails: new Set(),
  faviconLevel: null,
  networksKey: null,
  networks: [],
  radarKey: null,
  radarChart: null,
  acscKey: null,
  gridKey: null,
  gridChart: null,
  telcosKey: null,
  blueskyKey: null,
  bluesky: [],
};

// --- Small DOM helpers ---

const $ = (selector) => document.querySelector(selector);

function h(tag, props = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props)) {
    if (value == null || value === false) continue;
    if (key === "class") node.className = value;
    else if (key === "text") node.textContent = value;
    else node.setAttribute(key, value === true ? "" : value);
  }
  for (const child of children.flat()) {
    if (child == null || child === false) continue;
    node.append(child instanceof Node ? child : String(child));
  }
  return node;
}

function icon(name, size = 16) {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", "0 0 16 16");
  svg.setAttribute("width", size);
  svg.setAttribute("height", size);
  svg.setAttribute("aria-hidden", "true");
  svg.classList.add("icon");
  svg.innerHTML = ICON_PATHS[name] || ICON_PATHS.dots;
  return svg;
}

function pill(level, label, small = false) {
  const info = LEVELS[level] || LEVELS.unknown;
  return h("span", { class: `pill lvl-${level}${small ? " sm" : ""}` },
    icon(info.icon, small ? 12 : 14), h("span", { text: label || info.label }));
}

function safeUrl(value) {
  if (!value) return null;
  try {
    const url = new URL(value, location.href);
    return url.protocol === "https:" || url.protocol === "http:" ? url.href : null;
  } catch {
    return null;
  }
}

function sentence(text) {
  const t = (text || "").trim();
  return !t || /[.!?]$/.test(t) ? t : `${t}.`;
}

// --- Time ---

const timeFormat = new Intl.DateTimeFormat(undefined, { hour: "2-digit", minute: "2-digit" });
const clockFormat = new Intl.DateTimeFormat(undefined, { weekday: "short", hour: "2-digit", minute: "2-digit" });

const nowSeconds = () => Date.now() / 1000 + app.skew;
const clockTime = (ts) => timeFormat.format(new Date(ts * 1000));

function duration(seconds) {
  const s = Math.max(0, seconds);
  if (s < 90) return `${Math.round(s)} s`;
  const minutes = Math.round(s / 60);
  if (minutes < 90) return `${minutes} min`;
  const hours = Math.floor(minutes / 60);
  if (hours < 48) return minutes % 60 ? `${hours} h ${minutes % 60} min` : `${hours} h`;
  return `${Math.round(hours / 24)} days`;
}

function ago(ts) {
  const s = nowSeconds() - ts;
  return s < 10 ? "just now" : `${duration(s)} ago`;
}

// --- Fetching ---

async function getJSON(path) {
  const response = await fetch(path, { cache: "no-store" });
  if (!response.ok) throw new Error(`HTTP ${response.status}`);
  return response.json();
}

async function refreshState() {
  try {
    const data = await getJSON("api/state");
    app.skew = data.server_time - Date.now() / 1000;
    app.state = data;
    app.lastContact = Date.now();
    app.serverDown = false;
  } catch {
    app.serverDown = true;
  }
  render();
}

async function refreshSlow() {
  try {
    const [uptime, timeline] = await Promise.all([
      getJSON(`api/uptime?hours=${STRIP_HOURS}&bucket_minutes=${STRIP_BUCKET_MINUTES}`),
      getJSON("api/timeline?hours=24"),
    ]);
    app.uptime = uptime;
    app.timeline = timeline;
    renderUptime();
    renderTimeline();
  } catch {
    app.serverDown = true;
    renderServerBanner();
  }
}

// --- Rendering ---

function render() {
  $("#clock").textContent = clockFormat.format(new Date());
  renderServerBanner();
  if (!app.state) return;
  renderSimulation(app.state.simulation);
  renderOverall(app.state.overall);
  renderGaps(app.state.gaps || []);
  renderCategories(app.state.categories);
  renderStories(app.state.sources);
  renderSources(app.state.sources);
  renderNetworks(app.state.sources);
  renderRadar(app.state.sources);
  renderTelcos(app.state.sources);
  renderBluesky(app.state.sources);
  renderAcsc(app.state.sources);
  renderGrid(app.state.sources);
  renderFooter();
  updateTitleAndFavicon(app.state.overall.level);
}

function renderServerBanner() {
  const banner = $("#server-banner");
  document.body.classList.toggle("disconnected", app.serverDown);
  banner.hidden = !app.serverDown;
  if (!app.serverDown) return;
  banner.textContent = app.lastContact
    ? `Can't reach the sitrep server. Showing what it said at ${timeFormat.format(new Date(app.lastContact))}. Retrying every 15 seconds.`
    : "Can't reach the sitrep server yet. Retrying every 15 seconds.";
}

// Only during `python -m sitrep simulate`: say loudly that nothing here is real.
function renderSimulation(sim) {
  document.body.classList.toggle("simulation", Boolean(sim));
  $("#sim-banner").hidden = $("#sim-card").hidden = !sim;
  if (!sim) return;
  $("#sim-banner").replaceChildren(
    h("strong", { text: "\u{1F9EA} SIMULATION: nothing on this page is real" }),
    h("span", { class: "sim-step", text: ` · Step ${sim.step} of ${sim.steps}` }));
  $("#sim-title").textContent = `Step ${sim.step} of ${sim.steps}: ${sim.title}`;
  $("#sim-explain").textContent = sim.explain;
  const next = sim.next_at ? Math.max(0, sim.next_at - nowSeconds()) : null;
  $("#sim-next").textContent = sim.finished ? "" : next === null ? "" : `Next step in about ${duration(next)}.`;
}

function renderOverall(overall) {
  const info = OVERALL[overall.level] || OVERALL.unknown;
  $("#overall").className = `hero card lvl-${overall.level}`;
  $("#overall-icon").replaceChildren(icon(info.icon, 40));
  $("#overall-label").textContent = info.label;
  $("#overall-reason").textContent = sentence(overall.reason);
  const meta = [];
  if (overall.level !== "unknown") {
    meta.push(`Since ${clockTime(overall.since)} (${duration(nowSeconds() - overall.since)})`);
  }
  if (overall.easing) {
    const to = (OVERALL[overall.easing.to] || {}).label || overall.easing.to;
    meta.push(`Calming down: changes to "${to}" at ${clockTime(overall.easing.at)} if nothing new happens`);
  }
  $("#overall-meta").textContent = meta.join(" · ");
  const broken = (app.state.alerts || []).filter((a) => a.state === "problem");
  const warn = $("#overall-alerts");
  warn.hidden = !broken.length;
  warn.textContent = broken.map((a) => `${a.name} alerts aren't working: ${sentence(a.detail)}`).join(" ");
}

// What sitrep can't currently see: sources that are fetch failing, frozen or still learning.
function renderGaps(gaps) {
  $("#gaps").hidden = !gaps.length;
  $("#gaps-list").replaceChildren(...gaps.map((gap) =>
    h("li", { class: `gap lvl-${gap.health}` }, icon(LEVELS[gap.health]?.icon || "dots", 14),
      h("span", { text: sentence(gap.text) }))));
}

function renderCategories(categories) {
  $("#categories").replaceChildren(...categories.map((cat) => {
    const level = cat.level || "none";
    return h("article", { class: `cat card lvl-${level}` },
      h("div", { class: "cat-head" },
        h("h3", { text: cat.label }),
        cat.level ? pill(cat.level, null, true)
          : h("span", { class: "pill sm lvl-none", text: cat.sources ? "No live data" : "Not set up" })),
      h("p", { class: "cat-reason", text: sentence(cat.reason) }));
  }));
}

// Stories: items from official, news and social sources about one event. Triangulated ones
// (all three kinds) come first. Post text is shown here but never saved by sitrep.
const KIND_LABELS = { official: "Official", mainstream: "News", social: "Social" };

function storyCard(story) {
  const members = story.members.slice(0, 6).map((m) => {
    const link = m.type === "mainstream" ? safeUrl(m.url) : null;
    const who = m.outlet || m.source_name;
    const text = m.type === "social" ? `“${m.title}”` : m.title;
    return h("li", {}, h("span", { class: "muted", text: `${who}: ` }),
      link ? h("a", { href: link, target: "_blank", rel: "noopener noreferrer", text }) : text);
  });
  if (story.size > members.length) members.push(h("li", { class: "muted", text: `and ${story.size - members.length} more` }));
  return h("article", { class: `story card lvl-${story.level === "normal" ? "info" : story.level}` },
    h("header", { class: "story-head" },
      pill(story.level === "normal" ? "info" : story.level, story.triangulated ? "Triangulated" : null, true),
      h("h3", { text: story.title })),
    h("p", { class: "story-meta", text:
      `${story.where} · ${story.kinds.map((k) => KIND_LABELS[k]).join(" + ")} · ${ago(story.last_at)}` +
      (story.keywords.length ? ` · matches ${story.keywords.join(", ")}` : "") }),
    h("ul", { class: "story-members" }, ...members));
}

function renderStories(sources) {
  const stories = (sources.find((s) => s.id === "stories")?.details?.stories || []).filter((s) => s.kinds.length >= 2);
  $("#stories-block").hidden = !stories.length;
  if (stories.length) $("#stories").replaceChildren(...stories.map(storyCard));
}

function renderSources(sources) {
  const sorted = [...sources].sort((a, b) =>
    CATEGORY_ORDER.indexOf(a.category) - CATEGORY_ORDER.indexOf(b.category) || a.name.localeCompare(b.name));
  const box = $("#sources");
  if (!sorted.length) {
    box.replaceChildren(h("p", { class: "empty", text: "No sources are switched on in config.yaml." }));
    return;
  }
  box.replaceChildren(...sorted.map(sourceTile));
}

function sourceTile(source) {
  const parts = [
    h("header", { class: "tile-head" },
      h("div", {}, h("h3", { text: source.name }),
        h("p", { class: "tile-cat", text: CATEGORY_LABELS[source.category] || source.category })),
      pill(source.level)),
    h("p", { class: "tile-reason", text: sentence(source.reason) }),
  ];
  if (["failing", "frozen"].includes(source.level) && source.last_success_at) {
    parts.push(h("p", { class: "tile-lastknown" },
      "Last known:", pill(source.last_known_level, null, true), sentence(source.last_known_reason)));
  } else if (source.failures > 0) {
    const what = source.failures === 1 ? "The last attempt" : `The last ${source.failures} attempts`;
    parts.push(h("p", { class: "tile-warn", text: `${what} failed (${source.last_error}). Retrying.` }));
  }
  const meta = h("p", { class: "tile-meta" },
    source.last_success_at ? `Updated ${clockTime(source.last_success_at)} · ${ago(source.last_success_at)}` : "No update yet");
  const age = dataAge(source);
  if (age) meta.append(" · ", age);
  const link = safeUrl(source.link);
  if (link) meta.append(" · ", h("a", { href: link, target: "_blank", rel: "noopener noreferrer", text: "Source ↗" }));
  parts.push(meta);

  const extra = DETAILS[source.id];
  if (extra && source.details && Object.keys(source.details).length) parts.push(detailsBlock(source.id, extra.title, extra.render(source.details)));
  return h("article", { class: `tile card lvl-${source.level}` }, ...parts);
}

// How new the data inside the source's last answer was: not the same as when it was fetched.
function dataAge(source) {
  if (!source.last_success_at) return "";
  if (source.content_at) return `newest data ${ago(source.content_at)}`;
  if (source.feed_at) return `feed updated ${ago(source.feed_at)}`;
  return source.content_budget ? "data age unknown" : "";
}

function detailsBlock(id, title, content) {
  const block = h("details", { class: "tile-details", open: app.openDetails.has(id) }, h("summary", { text: title }), content);
  block.addEventListener("toggle", () => {
    if (block.open) app.openDetails.add(id);
    else app.openDetails.delete(id);
  });
  return block;
}

// Extra, source-specific detail shown under a tile.
const DETAILS = {
  probes: {
    title: "Show each check",
    render(details) {
      const checks = [...(details.checks || [])].sort((a, b) => CHECK_ORDER.indexOf(a.kind) - CHECK_ORDER.indexOf(b.kind));
      const rows = checks.map((c) => h("tr", {},
        h("td", {}, pill(c.ok ? "normal" : "critical", c.ok ? "OK" : "Failed", true)),
        h("td", { text: c.name }),
        h("td", { class: "type muted", text: CHECK_KINDS[c.kind] + (c.isp ? " (ISP)" : "") }),
        h("td", { class: "num", text: c.ok ? `${Math.round(c.ms)} ms` : c.error || "failed" })));
      return h("table", { class: "checks" },
        h("thead", {}, h("tr", {},
          h("th", { text: "Result" }), h("th", { text: "Check" }),
          h("th", { class: "type", text: "Type" }), h("th", { class: "num", text: "Time" }))),
        h("tbody", {}, rows));
    },
  },
  reach: {
    title: "Show each region",
    render(details) {
      const rows = (details.regions || []).map((r) => h("tr", {},
        h("td", {}, pill(...REGION_STATES[r.state] || REGION_STATES.unknown, true)),
        h("td", { text: r.name + (r.home ? " (home)" : "") }),
        h("td", {}, ...r.hosts.flatMap((host, i) => [i ? h("br") : "", reachHost(host)]))));
      return h("table", { class: "checks" },
        h("thead", {}, h("tr", {},
          h("th", { text: "State" }), h("th", { text: "Region" }), h("th", { text: "Handshake time (usual)" }))),
        h("tbody", {}, rows));
    },
  },
};

// NSW RFS incidents near home. "mark" is how much each matters here; "level" is the RFS's own alert level.
DETAILS.rfs = {
  title: "Show incidents near home",
  render(details) {
    const near = details.near || [];
    const parts = [];
    if (near.length) {
      parts.push(h("table", { class: "checks" },
        h("thead", {}, h("tr", {},
          h("th", { text: "Alert level" }), h("th", { text: "Incident" }), h("th", { class: "num", text: "Away" }))),
        h("tbody", {}, ...near.map((i) => h("tr", {},
          h("td", {}, pill(i.mark === "watch" ? "watch" : i.mark, i.level, true)),
          h("td", { text: [i.title, i.council, i.status].filter(Boolean).join(" · ") }),
          h("td", { class: "num", text: `${Math.round(i.km)} km` }))))));
    } else {
      parts.push(h("p", { class: "muted", text: `No incidents within ${details.show_km} km of ${details.home?.name || "home"}.` }));
    }
    const sw = details.statewide || {};
    parts.push(h("p", { class: "muted", text:
      `Across NSW: ${sw.total ?? 0} incidents (${sw["Emergency Warning"] ?? 0} Emergency Warning, ` +
      `${sw["Watch and Act"] ?? 0} Watch and Act, ${sw.Advice ?? 0} Advice). Data: NSW RFS, CC BY 4.0.` }));
    return h("div", {}, ...parts);
  },
};

// Live Traffic hazards near home: unplanned or major ones, nearest first.
DETAILS.livetraffic = {
  title: "Show hazards near home",
  render(details) {
    const near = details.near || [];
    const parts = [];
    if (near.length) {
      parts.push(h("table", { class: "checks" },
        h("thead", {}, h("tr", {},
          h("th", { text: "Hazard" }), h("th", { text: "Where" }), h("th", { class: "num", text: "Away" }))),
        h("tbody", {}, ...near.map((z) => h("tr", {},
          h("td", {}, pill(z.major ? "watch" : "info", z.kind === "majorevent" ? "Event" : z.major ? "Major" : "Hazard", true)),
          h("td", { text: [z.title, z.advice, z.updated ? `updated ${ago(z.updated)}` : ""].filter(Boolean).join(" · ") }),
          h("td", { class: "num", text: `${Math.round(z.km)} km` }))))));
    } else {
      parts.push(h("p", { class: "muted", text: `No unplanned or major hazards within ${details.show_km} km of home.` }));
    }
    parts.push(h("p", { class: "muted", text: "Data: Live Traffic NSW, Transport for NSW, CC BY 4.0." }));
    return h("div", {}, ...parts);
  },
};

// Sydney Trains and Metro: unplanned disruptions now, and how much planned trackwork is on.
DETAILS.transit = {
  title: "Show disruptions",
  render(details) {
    const rows = (details.disruptions || []).map((a) => h("tr", {},
      h("td", {}, pill(a.widespread && a.suspension ? "elevated" : "info",
        a.suspension ? "Suspended" : "Disrupted", true)),
      h("td", { text: [(a.line_names || []).join(", ") || "Network", a.header].filter(Boolean).join(": ") })));
    const parts = [rows.length
      ? h("table", { class: "checks" }, h("tbody", {}, ...rows))
      : h("p", { class: "muted", text: "No unplanned disruptions right now." })];
    const now = details.trackwork_now || [];
    parts.push(h("p", { class: "muted", text:
      `${details.planned_total ?? 0} planned trackwork alerts published; ${now.length} in force now` +
      (now.length ? `: ${now.slice(0, 3).map((t) => t.header).join("; ")}` : "") + ". Data: Transport for NSW, CC BY 4.0." }));
    return h("div", {}, ...parts);
  },
};

// BoM warnings in force: Sydney's first, then the rest of NSW.
DETAILS.bom = {
  title: "Show warnings",
  render(details) {
    const warnings = details.warnings || [];
    if (!warnings.length) return h("p", { class: "muted", text: "No BoM warnings in force in NSW. Data: Bureau of Meteorology." });
    return h("div", {},
      h("table", { class: "checks" }, h("tbody", {}, ...warnings.map((w) => h("tr", {},
        h("td", {}, pill(w.kind === "info" ? "info" : w.kind, w.sydney ? "Sydney" : "NSW", true)),
        h("td", { text: w.headline + (w.expires ? ` · until ${clockFormat.format(new Date(w.expires * 1000))}` : "") }))))),
      h("p", { class: "muted", text: "Data: Bureau of Meteorology, for personal use only." }));
  },
};

// The latest headlines, with the Sydney places they name. Links go to the outlet.
DETAILS.news = {
  title: "Show latest headlines",
  render(details) {
    const items = (details.headlines || []).map((n) => {
      const link = safeUrl(n.link);
      const title = link ? h("a", { href: link, target: "_blank", rel: "noopener noreferrer", text: n.title }) : n.title;
      const meta = [n.outlet, n.places?.length ? n.places.join(", ") : "", n.published ? ago(n.published) : ""]
        .filter(Boolean).join(" · ");
      return h("li", {}, title, h("span", { class: "muted", text: ` (${meta})` }));
    });
    return h("div", {},
      items.length ? h("ul", { class: "headlines" }, ...items) : h("p", { class: "muted", text: "No headlines yet." }),
      h("p", { class: "muted", text: "Headlines only; sitrep never fetches the articles." }));
  },
};

// Every story with two or more items, including ones from only one kind of source.
DETAILS.stories = {
  title: "Show every story",
  render(details) {
    const stories = details.stories || [];
    if (!stories.length) return h("p", { class: "muted", text: `No two items from the last ${details.window_hours} hours are about the same thing yet.` });
    return h("ul", { class: "headlines" }, ...stories.map((s) => h("li", {},
      `${s.title} `, h("span", { class: "muted", text: `(${s.where}; ${s.size} items: ${
        s.kinds.length ? s.kinds.map((k) => KIND_LABELS[k]).join(" + ") : "routine items only"})` }))));
  },
};

// Emerging terms: flagged ones first (suburbs before other words), then the busiest.
DETAILS.terms = {
  title: "Show busiest terms",
  render(details) {
    const terms = details.terms || [];
    if (!terms.length) return h("p", { class: "muted", text: "No terms counted yet." });
    const rows = terms.map((t) => h("tr", {},
      h("td", {}, t.flagged ? pill("elevated", "Busy", true) : h("span", { class: "muted", text: "" })),
      h("td", {}, h("strong", { text: t.term }), t.place ? h("span", { class: "muted", text: " (suburb)" }) : "",
        ...(t.samples || []).slice(0, 3).map((s) => h("div", { class: "muted term-sample",
          text: `${s.kind === "social" ? "Post" : "Headline"}: ${s.text}` }))),
      h("td", { class: "num", text: `${t.mentions} (usual ${t.usual})` })));
    return h("div", {},
      h("table", { class: "checks" }, h("thead", {}, h("tr", {}, h("th", { text: "" }), h("th", { text: "Term" }),
        h("th", { class: "num", text: `Mentions, ${details.window_hours} h` }))), h("tbody", {}, ...rows)),
      h("p", { class: "muted", text: "Only terms and counts are kept; post text shown here is never saved." }));
  },
};

// Foreign governments' travel advice for Australia, the FCDO's latest updates, and the embassy link.
const yearDate = new Intl.DateTimeFormat(undefined, { day: "numeric", month: "short", year: "numeric" });

// "3 hours ago" for the last week, then the date.
function agoOrDate(ts) {
  return nowSeconds() - ts <= 7 * 86400 ? ago(ts) : yearDate.format(new Date(ts * 1000));
}

function outLink(url, text) {
  const link = safeUrl(url);
  return link ? h("a", { href: link, target: "_blank", rel: "noopener noreferrer", text }) : text;
}

DETAILS.advice = {
  title: "Show advice and updates",
  render(details) {
    const parts = [];
    const rows = [];
    if (details.us) {
      rows.push(h("tr", {},
        h("td", {}, pill(details.us.level > 1 ? "elevated" : "normal", `Level ${details.us.level}`, true)),
        h("td", {}, outLink(details.us.link, "US State Department"),
          h("span", { class: "muted", text: ` · ${details.us.label}${details.us.issued ? ` · issued ${details.us.issued}` : ""}` }))));
    }
    if (details.uk) {
      rows.push(h("tr", {},
        h("td", {}, pill(details.uk.rank ? "elevated" : "normal", details.uk.rank ? "Against travel" : "No warning", true)),
        h("td", {}, outLink(details.uk.link, "UK FCDO"), h("span", { class: "muted", text: ` · ${details.uk.label}` }))));
    }
    if (rows.length) parts.push(h("table", { class: "checks" }, h("tbody", {}, ...rows)));
    const changes = (details.changes || []).filter((c) => c.counts);
    if (changes.length) {
      parts.push(h("ul", { class: "headlines" }, ...changes.map((c) => h("li", {},
        pill(c.raised ? "critical" : "info", c.raised ? "Raised" : "Lowered", true), ` ${c.text} `,
        h("span", { class: "muted", text: `(${ago(c.at)})` })))));
    }
    const updates = details.updates || [];
    if (updates.length) {
      parts.push(h("p", { class: "muted", text: "The FCDO's latest updates:" }));
      parts.push(h("ul", { class: "headlines" }, ...updates.map((u) => h("li", {},
        u.warnings.length && u.recent ? pill("elevated", "Security", true) : "", u.warnings.length && u.recent ? " " : "",
        u.note, h("span", { class: "muted", text: ` (${agoOrDate(u.at)}${u.warnings.length ? `; mentions ${u.warnings.join(", ")}` : ""})` })))));
    }
    parts.push(h("p", { class: "muted" }, "Security alerts from the US Embassy in Canberra: ",
      outLink(details.embassy, "au.usembassy.gov"), " (link only; its site refuses automated requests)."));
    parts.push(h("p", { class: "muted", text: "Data: US Department of State; UK FCDO via GOV.UK, " +
      "contains public sector information licensed under the Open Government Licence v3.0." }));
    return h("div", {}, ...parts);
  },
};

// Cloud and CDN status: each provider's Sydney region, its incidents, and Cloudflare maintenance.
DETAILS.cloud = {
  title: "Show each provider",
  render(details) {
    const rows = (details.providers || []).map((p) => h("tr", {},
      h("td", {}, p.incidents.length ? pill("elevated", "Incident", true) : pill("normal", "OK", true)),
      h("td", {}, outLink(p.link, p.name), h("span", { class: "muted", text: ` · ${p.region}` }),
        ...p.incidents.map((i) => h("div", {}, outLink(i.link, i.title),
          h("span", { class: "muted", text: ` (${[i.status, i.started ? `posted ${ago(i.started)}` : ""].filter(Boolean).join("; ")})` }))),
        ...p.maintenance.map((m) => h("div", { class: "muted", text: `Scheduled maintenance: ${m.name}` })),
        p.elsewhere ? h("div", { class: "muted", text: `${p.elsewhere} other incident${p.elsewhere === 1 ? "" : "s"} elsewhere` }) : "")));
    return h("div", {},
      h("table", { class: "checks" }, h("tbody", {}, ...rows)),
      h("p", { class: "muted", text: "From each provider's public status feed. Azure's lists only widespread incidents." }));
  },
};

// Australian ransomware victims, newest first. ransomware.live asks to be credited with a link.
DETAILS.ransomware = {
  title: "Show latest victims",
  render(details) {
    const victims = details.victims || [];
    const credit = h("p", { class: "muted" }, outLink(details.site, details.attribution || "Source: Ransomware.live"),
      ` · ${details.last_24h ?? 0} in the last 24 hours, usually ${details.usual_per_day ?? 0} a day. ` +
      "A listing is a gang's claim, often weeks after the attack.");
    if (!victims.length) return h("div", {}, h("p", { class: "muted", text: "No Australian victims listed recently." }), credit);
    return h("div", {},
      h("table", { class: "checks" }, h("tbody", {}, ...victims.map((v) => h("tr", {},
        h("td", {}, v.critical && v.recent ? pill("elevated", v.sector, true) : h("span", { class: "muted", text: v.sector })),
        h("td", {}, outLink(v.link, v.name), h("span", { class: "muted", text: ` · ${v.group} · ${agoOrDate(v.discovered)}` })))))),
      credit);
  },
};

// Fuel: median prices near home and across NSW, against the last 30 days.
DETAILS.fuel = {
  title: "Show prices",
  render(details) {
    const cents = (v) => (v == null ? "–" : v.toFixed(1));
    const rows = (details.fuels || []).flatMap((f) => ["home", "nsw"].filter((a) => f[a]).map((a) => h("tr", {},
      h("td", {}, f[a].jumped ? pill("elevated", "Jump", true) : h("span", { class: "muted", text: "" })),
      h("td", { text: `${f.name}, ${a === "home" ? `within ${details.near_km} km of home` : "across NSW"}` }),
      h("td", { class: "num", text: cents(f[a].cents) }),
      h("td", { class: "num muted", text: f[a].usual == null ? "learning" : `${cents(f[a].usual)} / ${cents(f[a].highest)}` }),
      h("td", { class: "num muted", text: String(f[a].stations) }))));
    const cheapest = Object.entries(details.cheapest || {}).map(([code, c]) => {
      const name = (details.fuels || []).find((f) => f.code === code)?.name || code;
      return `${name}: ${c.name}, ${cents(c.cents)}`;
    });
    return h("div", {},
      h("table", { class: "checks" },
        h("thead", {}, h("tr", {}, h("th", { text: "" }), h("th", { text: "Median price (c/L)" }), h("th", { class: "num", text: "Now" }),
          h("th", { class: "num", text: `Usual / highest, ${details.baseline_days} days` }), h("th", { class: "num", text: "Stations" }))),
        h("tbody", {}, ...rows)),
      cheapest.length ? h("p", { class: "muted", text: `Cheapest near home: ${cheapest.join("; ")}.` }) : "",
      h("p", { class: "muted", text: `A jump is more than ${details.jump_cents} c/L above the highest of the last ${details.baseline_days} days. ${details.credit || ""}.` }));
  },
};

// Aircraft over Sydney: the count against what's usual, and anything circling.
DETAILS.adsb = {
  title: "Show aircraft",
  render(details) {
    const parts = [h("p", {}, `${details.airborne} in the air within ${details.radius_km} km of the CBD` +
      (details.usual != null ? ` (usually ${Math.round(details.usual)} at ${details.basis})` : " (learning what's usual)") +
      `, ${details.helicopters === 1 ? "1 of them a helicopter" : `${details.helicopters || "none"} of them helicopters`}; ` +
      `${details.on_ground} on the ground.`)];
    if (details.check) {
      parts.push(h("p", { class: "muted", text: details.check.count != null
        ? `Double-checked with adsb.fi ${ago(details.check.at)}: it sees ${details.check.count}.`
        : `Couldn't double-check with adsb.fi (${details.check.error}).` }));
    }
    const orbits = details.orbits || [];
    parts.push(orbits.length
      ? h("ul", { class: "headlines" }, ...orbits.map((o) => h("li", {},
        `${o.helicopter ? "Helicopter" : "Aircraft"} ${o.name} circling near ${o.place} `,
        h("span", { class: "muted", text: `(${Math.round(o.minutes)} minutes, ${Math.round(o.low_ft)}–${Math.round(o.high_ft)} ft)` }))))
      : h("p", { class: "muted", text: "Nothing circling. Not every police aircraft broadcasts, so this means little." }));
    parts.push(h("p", { class: "muted" }, "Aircraft data: ", outLink("https://adsb.lol/", "adsb.lol"), " (ODbL 1.0) and ",
      outLink("https://adsb.fi/", "adsb.fi"), "."));
    return h("div", {}, ...parts);
  },
};

// Convergence: areas with several kinds of event at once.
DETAILS.convergence = {
  title: "Show areas",
  render(details) {
    const spots = details.hotspots || [];
    if (!spots.length) return h("p", { class: "muted", text: `No area has three kinds of event in the last ${details.window_hours} hours.` });
    return h("ul", { class: "headlines" }, ...spots.map((s) => h("li", {},
      h("strong", { text: s.where }), ` (${s.kinds.join(", ")}): `,
      h("span", { class: "muted", text: s.events.slice(0, 4).map((e) => e.title).join("; ") }))));
  },
};

const REGION_STATES = {
  normal: ["normal", "Normal"], degraded: ["elevated", "Degraded"], unreachable: ["critical", "Unreachable"],
  unknown: ["unknown", "Unknown"],
};

function reachHost(host) {
  const ms = host.now_ms ?? null;
  const usual = host.usual_ms ? ` (usual ${Math.round(host.usual_ms)})` : "";
  const what = {
    down: `down: ${host.error || "no answer"}`,
    lossy: `losing packets (${Math.round((host.loss || 0) * 100)}% of tries)`,
    unknown: `couldn't check: ${host.error || "unknown"}`,
    waiting: "waiting for results",
  }[host.state] || (ms === null ? "waiting for results" : `${Math.round(ms)} ms${usual}${host.state === "slow" ? ", slow" : ""}`);
  return `${host.provider}: ${what}`;
}

// --- Australian networks (from IODA) ---

const LIVE = ["normal", "elevated", "critical"];

function renderNetworks(sources) {
  const source = sources.find((s) => s.id === "ioda");
  const entities = source?.details?.entities || [];
  $("#networks-block").hidden = !entities.length;
  if (!entities.length) return;
  // Only redraw when IODA's data changes, so a chart being hovered isn't replaced under the pointer.
  const key = `${source.observed_at}|${source.level}`;
  if (key === app.networksKey) return;
  app.networksKey = key;
  app.networks = entities;
  const stale = !LIVE.includes(source.level);
  $("#networks-card").classList.toggle("is-stale", stale);
  $("#networks").replaceChildren(...entities.map(networkRow));
  const at = source.details.charts_at;
  $("#networks-meta").textContent = stale
    ? `IODA hasn't updated for a while, so this is what it last said.`
    : at ? `Charts updated ${clockTime(at)}.` : "Charts load in a few minutes.";
}

function networkRow(entity, i) {
  const asns = (entity.asns || []).map((n) => `AS${n}`).join(", ");
  return h("div", { class: `net lvl-${entity.level}`, "data-i": i },
    h("div", { class: "net-name" },
      h("p", {}, h("strong", { text: entity.name }), entity.home ? h("span", { class: "net-tag", text: "your network" }) : null),
      entity.detail ? h("p", { class: "net-detail", text: entity.detail })
        : asns ? h("p", { class: "net-detail muted", text: asns }) : null),
    h("div", { class: "net-now" }, pill(entity.level, entity.label, true)),
    networkChart(entity));
}

function svgNode(tag, attrs) {
  const node = document.createElementNS("http://www.w3.org/2000/svg", tag);
  for (const [key, value] of Object.entries(attrs)) node.setAttribute(key, value);
  return node;
}

function networkChart(entity) {
  const box = h("div", { class: "net-chart" });
  const values = entity.chart?.values || [];
  if (values.filter((v) => v != null).length < 2) {
    box.append(h("span", { class: "net-nochart", text: "No chart yet" }));
    return box;
  }
  const width = 100;
  const height = 30;
  const last = values.length - 1;
  const x = (i) => Math.min(Math.max((i / last) * width, 0), width);
  const y = (v) => (height - (Math.min(v, CHART_MAX) / CHART_MAX) * height).toFixed(2);
  let line = "";
  let dots = ""; // a lone reading between gaps has no neighbour to join, so it's drawn as a dot
  const gaps = [];
  let gapStart = null;
  values.forEach((v, i) => {
    if (v == null) {
      if (gapStart == null) gapStart = i;
      return;
    }
    if (gapStart != null) gaps.push([gapStart, i - 1]);
    gapStart = null;
    const joined = i > 0 && values[i - 1] != null;
    if (joined) line += `L${x(i).toFixed(2)} ${y(v)}`;
    else if (i < last && values[i + 1] != null) line += `M${x(i).toFixed(2)} ${y(v)}`;
    else dots += `M${x(i).toFixed(2)} ${y(v)}h0.01`;
  });
  // A gap still open at the end is only IODA catching up, so it isn't shaded.
  const svg = svgNode("svg", { viewBox: `0 0 ${width} ${height}`, preserveAspectRatio: "none", "aria-hidden": "true" });
  for (const [from, to] of gaps) {
    const left = x(from - 0.5);
    svg.append(svgNode("rect", { class: "gap", x: left.toFixed(2), y: 0, width: (x(to + 0.5) - left).toFixed(2), height }));
  }
  svg.append(
    svgNode("path", { class: "floor", d: `M0 ${height}H${width}`, "vector-effect": "non-scaling-stroke" }),
    svgNode("path", { class: "usual", d: `M0 ${y(1)}H${width}`, "vector-effect": "non-scaling-stroke" }),
    svgNode("path", { class: "line", d: line, "vector-effect": "non-scaling-stroke" }),
    svgNode("path", { class: "line dots", d: dots, "vector-effect": "non-scaling-stroke" }));
  const low = Math.min(...values.filter((v) => v != null));
  box.setAttribute("role", "img");
  box.setAttribute("aria-label", `${entity.name}: active probing over the last 24 hours, lowest ${Math.round(low * 100)}% of usual`);
  box.append(svg, h("span", { class: "net-guide", hidden: true }));
  return box;
}

function bindNetworkTooltip() {
  const list = $("#networks");
  const card = $("#networks-card");
  const tip = $("#nets-tip");
  let guide = null;
  const hide = () => {
    tip.hidden = true;
    if (guide) guide.hidden = true;
    guide = null;
  };
  const show = (event) => {
    const box = event.target.closest(".net-chart");
    const entity = box && app.networks[Number(box.closest(".net").dataset.i)];
    const chart = entity?.chart;
    if (!chart?.values?.length || !box.querySelector("svg")) return hide();
    const rect = box.getBoundingClientRect();
    const fraction = Math.min(Math.max((event.clientX - rect.left) / rect.width, 0), 1);
    const i = Math.round(fraction * (chart.values.length - 1));
    const value = chart.values[i];
    const start = chart.from + i * chart.step;
    tip.replaceChildren(
      h("strong", { text: `${entity.name} · ${clockTime(start)}` }),
      value == null ? "No data from IODA" : `${Math.round(value * 100)}% of usual`);
    if (guide && guide !== box.querySelector(".net-guide")) guide.hidden = true;
    guide = box.querySelector(".net-guide");
    guide.hidden = false;
    guide.style.left = `${(i / (chart.values.length - 1)) * 100}%`;
    const cardBox = card.getBoundingClientRect();
    tip.hidden = false;
    const half = tip.offsetWidth / 2 + 4;
    const x = rect.left + (i / (chart.values.length - 1)) * rect.width - cardBox.left;
    tip.style.left = `${Math.min(Math.max(x, half), cardBox.width - half)}px`;
    tip.style.top = `${rect.top - cardBox.top}px`;
  };
  list.addEventListener("pointermove", show);
  list.addEventListener("pointerdown", show);
  list.addEventListener("pointerleave", hide);
}

// --- Australian traffic (from Cloudflare Radar) ---

const dayFormat = new Intl.DateTimeFormat(undefined, { weekday: "short", hour: "2-digit", minute: "2-digit" });
const dateFormat = new Intl.DateTimeFormat(undefined, { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });
const percent = (ratio) => `${Math.round(ratio * 100)}%`;

function renderRadar(sources) {
  const source = sources.find((s) => s.id === "radar");
  const details = source?.details;
  $("#radar-block").hidden = !details?.traffic;
  if (!details?.traffic) return;
  const key = `${source.observed_at}|${source.level}`;
  if (key === app.radarKey) return;
  app.radarKey = key;
  const stale = !LIVE.includes(source.level);
  $("#radar-card").classList.toggle("is-stale", stale);

  const chart = details.chart;
  app.radarChart = chart;
  $("#radar-chart").replaceChildren(...(chart ? radarChart(chart) : [h("p", { class: "net-nochart", text: "No traffic numbers yet." })]));
  if (chart) {
    const last = chart.from + (chart.today.length - 1) * chart.step;
    $("#radar-start").textContent = dayFormat.format(new Date(chart.from * 1000));
    $("#radar-end").textContent = dayFormat.format(new Date(last * 1000));
  }

  $("#radar-carriers").replaceChildren(...details.traffic.filter((t) => t.kind === "carrier").flatMap((t) => {
    const chip = pill(t.level, t.ratio == null ? `${t.name}: no data` : `${t.name} ${percent(t.ratio)}`, true);
    chip.title = t.ratio == null ? "" : `${t.name}'s traffic is ${percent(t.ratio)} of the same time last week`;
    return t.home ? [chip, h("span", { class: "net-tag", text: "your network" })] : [chip];
  }));

  const reports = details.reports || [];
  $("#radar-reports").replaceChildren(...(reports.length ? reports.map(radarReport)
    : [h("li", { class: "empty" }, "None for Australia or the carriers.")]));

  const parts = [`Route hijacks involving Australia in the last day: ${details.hijack_count}`];
  parts[0] += details.hijacks?.length ? `, ${details.hijacks.length} serious (see above)` : ", none serious against these carriers";
  let leaks = `route leaks: ${details.leak_count}`;
  if (details.leak_carriers?.length) leaks += `, some through ${details.leak_carriers.join(", ")}`;
  $("#radar-routes").textContent = `${parts[0]}; ${leaks}. Route leaks are shown but not counted.`;

  const asOf = details.traffic.find((t) => t.kind === "country")?.as_of;
  $("#radar-meta").textContent = stale ? "Radar hasn't updated for a while, so this is what it last said."
    : asOf ? `Traffic numbers up to ${clockTime(asOf)}.` : "";
}

function radarReport(report) {
  const level = !report.ongoing ? "normal" : report.nationwide ? "critical" : "elevated";
  const when = report.ongoing || !report.end
    ? `Since ${dateFormat.format(new Date(report.start * 1000))}`
    : `${dateFormat.format(new Date(report.start * 1000))} – ${dateFormat.format(new Date(report.end * 1000))}`;
  const link = safeUrl(report.url);
  return h("li", {},
    pill(level, report.ongoing ? "Ongoing" : "Ended", true),
    h("div", {},
      h("strong", { text: report.who }), " ", report.description,
      h("span", { class: "when" }, when,
        link ? [" · ", h("a", { href: link, target: "_blank", rel: "noopener noreferrer", text: "Cloudflare's post ↗" })] : null)));
}

function radarChart(chart) {
  const width = 100;
  const height = 40;
  const series = [chart.today, chart.lastweek];
  const top = Math.max(1, ...series.flat().filter((v) => v != null)) * 1.05;
  const last = chart.today.length - 1;
  const x = (i) => ((i / last) * width).toFixed(2);
  const y = (v) => (height - (v / top) * height).toFixed(2);
  const path = (values) => {
    let d = "";
    values.forEach((v, i) => {
      if (v == null) return;
      d += `${i > 0 && values[i - 1] != null ? "L" : "M"}${x(i)} ${y(v)}`;
    });
    return d;
  };
  const svg = svgNode("svg", { viewBox: `0 0 ${width} ${height}`, preserveAspectRatio: "none", "aria-hidden": "true" });
  svg.append(
    svgNode("path", { class: "grid", d: `M0 ${y(top / 2)}H${width}`, "vector-effect": "non-scaling-stroke" }),
    svgNode("path", { class: "floor", d: `M0 ${height}H${width}`, "vector-effect": "non-scaling-stroke" }),
    svgNode("path", { class: "lastweek", d: path(chart.lastweek), "vector-effect": "non-scaling-stroke" }),
    svgNode("path", { class: "today", d: path(chart.today), "vector-effect": "non-scaling-stroke" }));
  const box = $("#radar-chart");
  box.dataset.top = top;
  box.setAttribute("role", "img");
  box.setAttribute("aria-label", "Cloudflare traffic for Australia over the last day, with the same time last week for comparison");
  return [svg, h("span", { class: "net-guide", hidden: true }),
    h("span", { class: "radar-dot lastweek", hidden: true }), h("span", { class: "radar-dot today", hidden: true })];
}

function bindRadarTooltip() {
  const box = $("#radar-chart");
  const card = $("#radar-card");
  const tip = $("#radar-tip");
  const parts = () => [box.querySelector(".net-guide"), ...box.querySelectorAll(".radar-dot")];
  const hide = () => {
    tip.hidden = true;
    for (const node of parts()) if (node) node.hidden = true;
  };
  const show = (event) => {
    const chart = app.radarChart;
    if (!chart?.today?.length || !box.querySelector("svg")) return hide();
    const rect = box.getBoundingClientRect();
    const last = chart.today.length - 1;
    const i = Math.round(Math.min(Math.max((event.clientX - rect.left) / rect.width, 0), 1) * last);
    const today = chart.today[i];
    const lastweek = chart.lastweek[i];
    const top = Number(box.dataset.top) || 1;
    const left = `${(i / last) * 100}%`;
    const [guide, weekDot, todayDot] = parts();
    guide.hidden = false;
    guide.style.left = left;
    for (const [dot, value] of [[todayDot, today], [weekDot, lastweek]]) {
      dot.hidden = value == null;
      dot.style.left = left;
      dot.style.top = `${(1 - (value ?? 0) / top) * 100}%`;
    }
    const line = today != null && lastweek ? `Today is ${percent(today / lastweek)} of the same time last week` : "No data for this time";
    tip.replaceChildren(h("strong", { text: dayFormat.format(new Date((chart.from + i * chart.step) * 1000)) }), line);
    const cardBox = card.getBoundingClientRect();
    tip.hidden = false;
    const half = tip.offsetWidth / 2 + 4;
    const cx = rect.left + (i / last) * rect.width - cardBox.left;
    tip.style.left = `${Math.min(Math.max(cx, half), cardBox.width - half)}px`;
    tip.style.top = `${rect.top - cardBox.top}px`;
  };
  box.addEventListener("pointermove", show);
  box.addEventListener("pointerdown", show);
  box.addEventListener("pointerleave", hide);
}

// --- What the telcos say (TPG, Aussie Broadband, and links to the rest) ---

function renderTelcos(sources) {
  const source = sources.find((s) => s.id === "telcos");
  const details = source?.details;
  $("#telcos-block").hidden = !details?.links;
  if (!details?.links) return;
  const key = `${source.observed_at}|${source.level}`;
  if (key === app.telcosKey) return;
  app.telcosKey = key;
  const stale = !LIVE.includes(source.level);
  const cards = (details.telcos || []).map((t) => telcoCard(t, details.state, stale));
  cards.push(h("article", { class: "card telco telco-links" },
    h("header", { class: "telco-head" }, h("h3", { text: cards.length ? "The others" : "Telco status pages" })),
    h("ul", { class: "telco-linklist" }, details.links.map((link) => {
      const url = safeUrl(link.url);
      return h("li", {},
        url ? h("a", { href: url, target: "_blank", rel: "noopener noreferrer", text: `${link.name} ↗` }) : link.name,
        link.tag ? h("span", { class: "net-tag", text: link.tag }) : null);
    })),
    h("p", { class: "telco-small", text: "These need your address, so check them yourself." })));
  $("#telcos").replaceChildren(...cards);
  $("#telcos-meta").textContent = stale ? "sitrep couldn't check the telcos recently, so this is what they last said." : "";
}

function telcoCard(telco, state, stale) {
  const outages = telco.unplanned || [];
  let level = telco.level;
  let label;
  if (telco.error) [level, label] = ["stale", "Couldn't check"];
  else if (stale) [level, label] = ["stale", "Not checked lately"];
  else if (level === "elevated") label = "Widespread outage";
  else label = telco.unplanned_count ? "Local outages only" : "No unplanned outages";
  const page = safeUrl(telco.page);
  const parts = [
    h("header", { class: "telco-head" },
      h("h3", {}, telco.name, telco.tag ? h("span", { class: "net-tag", text: telco.tag }) : null),
      pill(level, label, true)),
  ];
  if (telco.error) {
    parts.push(h("p", { class: "tile-warn", text: `The last check failed (${telco.error}). Retrying.` }));
  } else {
    parts.push(h("dl", { class: "telco-counts" },
      h("div", {}, h("dt", { text: "Unplanned outages" }),
        h("dd", { text: `${telco.unplanned_count}${telco.unplanned_count ? ` (${telco.unplanned_in_state || "none"} in ${state})` : ""}` })),
      h("div", {}, h("dt", { text: "Maintenance now" }),
        h("dd", { text: `${telco.planned_now}${telco.planned_now ? ` (${telco.planned_in_state} in ${state})` : ""}` }))));
    if (outages.length) {
      parts.push(h("ul", { class: "telco-outages" }, outages.map((o) => {
        const facts = [o.service, o.scope && !o.where.includes(o.scope) ? o.scope : null,
          o.customers ? `${wholeNumber.format(o.customers)} customers` : null, o.note || null,
          o.since ? `since ${dateFormat.format(new Date(o.since * 1000))}` : null].filter(Boolean);
        return h("li", {},
          o.widespread ? pill("elevated", "Widespread", true) : null,
          h("strong", { text: o.where }), h("span", { class: "telco-small", text: facts.join(" · ") }));
      })));
      if (telco.unplanned_count > outages.length) {
        parts.push(h("p", { class: "telco-small", text: `And ${telco.unplanned_count - outages.length} more on ${telco.name}'s page.` }));
      }
    }
  }
  if (page) parts.push(h("p", { class: "telco-small" }, h("a", { href: page, target: "_blank", rel: "noopener noreferrer", text: `${telco.name}'s status page ↗` })));
  return h("article", { class: `card telco lvl-${level}` }, ...parts);
}

// --- On Bluesky (keyword groups from Jetstream) ---

function renderBluesky(sources) {
  const source = sources.find((s) => s.id === "bluesky");
  const groups = source?.details?.groups;
  $("#bluesky-block").hidden = !groups?.length;
  if (!groups?.length) return;
  const key = `${source.observed_at}|${source.level}`;
  if (key === app.blueskyKey) return;
  app.blueskyKey = key;
  app.bluesky = groups;
  const details = source.details;
  const stale = !["normal", "elevated", "critical", "learning"].includes(source.level);
  $("#bluesky-card").classList.toggle("is-stale", stale);
  $("#bluesky-groups").replaceChildren(...groups.map((g, i) => blueskyRow(g, i, source.level, details.window_minutes)));
  const parts = [];
  if (details.posts_per_second) parts.push(`Reading about ${Math.round(details.posts_per_second)} posts a second.`);
  if (details.learning_left > 0) {
    parts.push(`Still learning what's usual, so spikes can't raise the alarm yet: ${duration(details.learning_left)} to go.`);
  }
  if (stale) parts.push("The Bluesky connection hasn't updated for a while, so this is what it last said.");
  $("#bluesky-meta").textContent = parts.join(" ");
}

function blueskyRow(group, i, sourceLevel, windowMinutes) {
  const learning = sourceLevel === "learning" || group.learning_left > 0;
  const [level, label] = learning ? ["learning", "Learning"]
    : group.spike ? ["elevated", "Spike"] : ["normal", "Normal"];
  const now = `${group.people} ${group.people === 1 ? "person" : "people"} in ${windowMinutes} min`;
  const usual = group.learning_left > 0 && sourceLevel !== "learning"
    ? ` · new group, learning for ${duration(group.learning_left)} more`
    : group.usual == null ? "" : `, usually about ${group.usual.toFixed(1)}`;
  const samples = group.samples || [];
  const row = h("div", { class: `net bluesky-row lvl-${level}`, "data-i": i },
    h("div", { class: "net-name" },
      h("p", {}, h("strong", { text: group.name })),
      h("p", { class: "net-detail", text: now + usual })),
    h("div", { class: "net-now" }, pill(level, label, true)),
    blueskyChart(group));
  if (!samples.length) return row;
  const list = h("ul", { class: "bluesky-posts" }, samples.map((post) => {
    const link = safeUrl(post.url);
    return h("li", {},
      h("p", { class: "bluesky-text", text: post.text }),
      h("p", { class: "telco-small" }, dateFormat.format(new Date(post.ts * 1000)),
        link ? [" · ", h("a", { href: link, target: "_blank", rel: "noopener noreferrer", text: "on Bluesky ↗" })] : null));
  }));
  return h("div", { class: "bluesky-group" }, row,
    detailsBlock(`bluesky-${group.id}`, `Recent posts (${samples.length})`, list));
}

function blueskyChart(group) {
  const box = h("div", { class: "net-chart bluesky-chart" });
  const values = group.chart?.values || [];
  if (!values.some((v) => v != null)) {
    box.append(h("span", { class: "net-nochart", text: "No counts yet" }));
    return box;
  }
  const width = 100;
  const height = 30;
  const usual15 = group.usual == null ? null : group.usual * 3; // the window is 5 minutes; bars are 15
  const top = Math.max(4, usual15 ? usual15 * 2 : 0, ...values.filter((v) => v != null)) * 1.1;
  const slot = width / values.length;
  const svg = svgNode("svg", { viewBox: `0 0 ${width} ${height}`, preserveAspectRatio: "none", "aria-hidden": "true" });
  values.forEach((v, i) => {
    if (v == null) {
      svg.append(svgNode("rect", { class: "gap", x: (i * slot).toFixed(2), y: 0, width: slot.toFixed(2), height }));
    } else if (v > 0) {
      const h_ = Math.max((v / top) * height, 1.5);
      svg.append(svgNode("rect", { class: "bar", x: (i * slot + slot * 0.15).toFixed(2), y: (height - h_).toFixed(2),
        width: (slot * 0.7).toFixed(2), height: h_.toFixed(2) }));
    }
  });
  svg.append(svgNode("path", { class: "floor", d: `M0 ${height}H${width}`, "vector-effect": "non-scaling-stroke" }));
  if (usual15) {
    const y = (height - (usual15 / top) * height).toFixed(2);
    svg.append(svgNode("path", { class: "usual", d: `M0 ${y}H${width}`, "vector-effect": "non-scaling-stroke" }));
  }
  const most = Math.max(...values.filter((v) => v != null));
  box.setAttribute("role", "img");
  box.setAttribute("aria-label", `${group.name}: at most ${most} ${most === 1 ? "person" : "people"} in any 15 minutes over the last day`);
  box.append(svg, h("span", { class: "net-guide", hidden: true }));
  return box;
}

function bindBlueskyTooltip() {
  const list = $("#bluesky-groups");
  const card = $("#bluesky-card");
  const tip = $("#bluesky-tip");
  let guide = null;
  const hide = () => {
    tip.hidden = true;
    if (guide) guide.hidden = true;
    guide = null;
  };
  const show = (event) => {
    const box = event.target.closest(".bluesky-chart");
    const group = box && app.bluesky[Number(box.closest(".net").dataset.i)];
    const chart = group?.chart;
    if (!chart?.values?.length || !box.querySelector("svg")) return hide();
    const rect = box.getBoundingClientRect();
    const n = chart.values.length;
    const i = Math.min(Math.floor(((event.clientX - rect.left) / rect.width) * n), n - 1);
    const value = chart.values[Math.max(i, 0)];
    const start = chart.from + Math.max(i, 0) * chart.step;
    tip.replaceChildren(
      h("strong", { text: `${group.name} · ${clockTime(start)}–${clockTime(start + chart.step)}` }),
      value == null ? "sitrep wasn't connected" : `${value} ${value === 1 ? "person" : "people"}`);
    if (guide && guide !== box.querySelector(".net-guide")) guide.hidden = true;
    guide = box.querySelector(".net-guide");
    guide.hidden = false;
    const x = ((Math.max(i, 0) + 0.5) / n) * 100;
    guide.style.left = `${x}%`;
    const cardBox = card.getBoundingClientRect();
    tip.hidden = false;
    const half = tip.offsetWidth / 2 + 4;
    const cx = rect.left + (x / 100) * rect.width - cardBox.left;
    tip.style.left = `${Math.min(Math.max(cx, half), cardBox.width - half)}px`;
    tip.style.top = `${rect.top - cardBox.top}px`;
  };
  list.addEventListener("pointermove", show);
  list.addEventListener("pointerdown", show);
  list.addEventListener("pointerleave", hide);
}

// --- Latest from the ACSC ---

const shortDate = new Intl.DateTimeFormat(undefined, { day: "numeric", month: "short" });

function renderAcsc(sources) {
  const source = sources.find((s) => s.id === "acsc");
  const items = source?.details?.items;
  $("#acsc-block").hidden = !items;
  if (!items) return;
  const key = `${source.observed_at}|${source.level}`;
  if (key === app.acscKey) return;
  app.acscKey = key;
  $("#acsc-year").textContent = new Date().getFullYear();
  $("#acsc-items").replaceChildren(...(items.length ? items.map(acscItem)
    : [h("li", { class: "empty", text: "ACSC's feeds are empty right now." })]));
  const hours = source.details.recent_hours;
  const signs = source.details.warning_signs || [];
  $("#acsc-meta").textContent = signs.length
    ? `Items from the last ${hours} hours that mention ${signs.slice(0, -1).join(", ")} or ${signs.at(-1)} raise the level.`
    : "";
}

function acscItem(item) {
  const link = safeUrl(item.link);
  const title = link ? h("a", { href: link, target: "_blank", rel: "noopener noreferrer", text: item.title }) : h("strong", { text: item.title });
  const badge = item.recent ? pill(item.signs.length ? "elevated" : "info", "New", true) : null;
  return h("li", { class: "acsc-item" },
    h("time", { class: "acsc-date", datetime: new Date(item.published * 1000).toISOString(), title: dateFormat.format(new Date(item.published * 1000)), text: shortDate.format(new Date(item.published * 1000)) }),
    h("div", {},
      h("div", { class: "acsc-head" }, badge, title, h("span", { class: "acsc-kind", text: item.kind })),
      item.summary ? h("p", { class: "acsc-summary", text: item.summary }) : null,
      item.signs.length ? h("div", { class: "acsc-signs" }, item.signs.map((s) => h("span", { class: "acsc-sign", text: s }))) : null));
}

// --- Electricity grid (from AEMO) ---

const wholeNumber = new Intl.NumberFormat(undefined, { maximumFractionDigits: 0 });
const megawatts = (value) => `${wholeNumber.format(value)} MW`;
const perMWh = (value) => `${value < 0 ? "−" : ""}$${wholeNumber.format(Math.abs(value))}/MWh`;

function renderGrid(sources) {
  const source = sources.find((s) => s.id === "aemo");
  const details = source?.details;
  $("#grid-block").hidden = !details?.now;
  if (!details?.now) return;
  const key = `${source.observed_at}|${source.level}`;
  if (key === app.gridKey) return;
  app.gridKey = key;
  const stale = !LIVE.includes(source.level);
  $("#grid-card").classList.toggle("is-stale", stale);
  const name = details.region_name;
  $("#grid-title").textContent = `${name} electricity grid`;

  const conditions = details.conditions || [];
  const chip = (c, text) => {
    const node = pill(c.level, text, true);
    node.title = sentence(c.meaning.charAt(0).toUpperCase() + c.meaning.slice(1));
    return node;
  };
  const home = conditions.filter((c) => c.home);
  const now = details.now;
  $("#grid-stats").replaceChildren(
    gridStat("Demand", now.demand == null ? "No figure" : megawatts(now.demand)),
    gridStat("Price", now.price == null ? "No figure" : perMWh(now.price)),
    gridStat("Supply", home.length ? home.map((c) => chip(c, c.label)) : pill("normal", "No warnings", true)));

  const chart = details.chart;
  app.gridChart = chart;
  $("#grid-chart").replaceChildren(...(chart ? gridChart(chart)
    : [h("p", { class: "net-nochart", text: "The chart fills in as AEMO's figures arrive." })]));
  if (chart) {
    $("#grid-start").textContent = dayFormat.format(new Date(chart.from * 1000));
    $("#grid-end").textContent = dayFormat.format(new Date((chart.from + (chart.today.length - 1) * chart.step) * 1000));
  }

  const away = conditions.filter((c) => !c.home && (c.level === "elevated" || c.level === "critical"));
  $("#grid-elsewhere").replaceChildren(...(away.length
    ? ["Elsewhere in the NEM: ", ...away.map((c) => chip(c, `${c.label}, ${c.region_name}`))]
    : ["Elsewhere in the NEM: no load shedding or serious supply shortfalls."]));

  const hours = details.notice_hours;
  $("#grid-notices-title").textContent = `AEMO notices about ${name}, last ${hours} hours`;
  const notices = details.notices || [];
  $("#grid-notices").replaceChildren(...(notices.length ? notices.map(gridNotice)
    : [h("li", { class: "empty", text: `None about ${name} or about load shedding elsewhere.` })]));

  const routine = details.notices_total - details.notices_shown_of;
  const parts = [];
  if (details.notices_shown_of > notices.length) parts.push(`Showing the latest ${notices.length} of ${details.notices_shown_of}.`);
  if (routine > 0) parts.push(`${routine} routine ${routine === 1 ? "notice" : "notices"} not shown.`);
  parts.push(stale ? "AEMO hasn't updated for a while, so this is what it last said."
    : `Figures for the 5 minutes to ${clockTime(now.at)}.`);
  $("#grid-meta").textContent = parts.join(" ");
}

function gridStat(label, value) {
  return h("div", { class: "grid-stat" },
    h("p", { class: "grid-stat-label", text: label }),
    h("div", { class: "grid-stat-value" }, value));
}

function gridNotice(notice) {
  const when = new Date(notice.time * 1000);
  return h("li", { class: "grid-notice" },
    h("time", { class: "acsc-date", datetime: when.toISOString(), title: dateFormat.format(when), text: dayFormat.format(when) }),
    h("div", {},
      h("div", { class: "acsc-head" }, pill(notice.level, notice.label, true), h("strong", { text: notice.title })),
      notice.summary ? h("p", { class: "acsc-summary", text: notice.summary }) : null,
      notice.body ? detailsBlock(`aemo-${notice.id}`, "Read the notice", h("p", { class: "grid-body", text: notice.body })) : null));
}

// Gridlines at round numbers of megawatts, about three of them.
function megawattStep(top) {
  return [250, 500, 1000, 2000, 2500, 5000, 10000].find((step) => top / step <= 4) || 20000;
}

function gridChart(chart) {
  const width = 100;
  const height = 40;
  const highest = Math.max(1, ...[...chart.today, ...chart.yesterday].filter((v) => v != null));
  const step = megawattStep(highest * 1.05);
  const top = Math.ceil((highest * 1.05) / step) * step;
  const last = chart.today.length - 1;
  const x = (i) => ((i / last) * width).toFixed(2);
  const y = (v) => (height - (v / top) * height).toFixed(2);
  const path = (values) => {
    let d = "";
    values.forEach((v, i) => {
      if (v == null) return;
      d += `${i > 0 && values[i - 1] != null ? "L" : "M"}${x(i)} ${y(v)}`;
    });
    return d;
  };
  const svg = svgNode("svg", { viewBox: `0 0 ${width} ${height}`, preserveAspectRatio: "none", "aria-hidden": "true" });
  const labels = [];
  for (let v = step; v < top; v += step) {
    svg.append(svgNode("path", { class: "grid", d: `M0 ${y(v)}H${width}`, "vector-effect": "non-scaling-stroke" }));
    labels.push(h("span", { class: "grid-ylabel", style: `top:${(1 - v / top) * 100}%`, text: megawatts(v) }));
  }
  svg.append(
    svgNode("path", { class: "floor", d: `M0 ${height}H${width}`, "vector-effect": "non-scaling-stroke" }),
    svgNode("path", { class: "lastweek", d: path(chart.yesterday), "vector-effect": "non-scaling-stroke" }),
    svgNode("path", { class: "today", d: path(chart.today), "vector-effect": "non-scaling-stroke" }));
  const box = $("#grid-chart");
  box.dataset.top = top;
  box.setAttribute("role", "img");
  const known = chart.today.filter((v) => v != null);
  box.setAttribute("aria-label",
    `Electricity demand over the last day, between ${megawatts(Math.min(...known))} and ${megawatts(Math.max(...known))}, with the same time yesterday for comparison`);
  return [svg, ...labels, h("span", { class: "net-guide", hidden: true }),
    h("span", { class: "radar-dot lastweek", hidden: true }), h("span", { class: "radar-dot today", hidden: true })];
}

function bindGridTooltip() {
  const box = $("#grid-chart");
  const card = $("#grid-card");
  const tip = $("#grid-tip");
  const parts = () => [box.querySelector(".net-guide"), ...box.querySelectorAll(".radar-dot")];
  const hide = () => {
    tip.hidden = true;
    for (const node of parts()) if (node) node.hidden = true;
  };
  const show = (event) => {
    const chart = app.gridChart;
    if (!chart?.today?.length || !box.querySelector("svg")) return hide();
    const rect = box.getBoundingClientRect();
    const last = chart.today.length - 1;
    const i = Math.round(Math.min(Math.max((event.clientX - rect.left) / rect.width, 0), 1) * last);
    const today = chart.today[i];
    const yesterday = chart.yesterday[i];
    const price = chart.price[i];
    const top = Number(box.dataset.top) || 1;
    const left = `${(i / last) * 100}%`;
    const [guide, yesterdayDot, todayDot] = parts();
    guide.hidden = false;
    guide.style.left = left;
    for (const [dot, value] of [[todayDot, today], [yesterdayDot, yesterday]]) {
      dot.hidden = value == null;
      dot.style.left = left;
      dot.style.top = `${(1 - (value ?? 0) / top) * 100}%`;
    }
    const lines = [h("strong", { text: dayFormat.format(new Date((chart.from + i * chart.step) * 1000)) })];
    lines.push(h("span", { class: "tip-line", text: today == null ? "No figure from AEMO"
      : `Demand ${megawatts(today)}${price == null ? "" : ` · ${perMWh(price)}`}` }));
    if (yesterday != null) lines.push(h("span", { class: "tip-line", text: `Yesterday ${megawatts(yesterday)}` }));
    tip.replaceChildren(...lines);
    const cardBox = card.getBoundingClientRect();
    tip.hidden = false;
    const half = tip.offsetWidth / 2 + 4;
    const cx = rect.left + (i / last) * rect.width - cardBox.left;
    tip.style.left = `${Math.min(Math.max(cx, half), cardBox.width - half)}px`;
    tip.style.top = `${rect.top - cardBox.top}px`;
  };
  box.addEventListener("pointermove", show);
  box.addEventListener("pointerdown", show);
  box.addEventListener("pointerleave", hide);
}

function renderUptime() {
  const data = app.uptime;
  if (!data) return;
  const { buckets, bucket_seconds: bucketSeconds, round_seconds: roundSeconds } = data;
  const strip = h("div", { class: "strip", style: `--cells:${buckets.length}` });
  buckets.forEach((bucket, i) => {
    const tone = !bucket.rounds ? "none" : bucket.dark ? "critical" : bucket.partial ? "elevated" : "normal";
    strip.append(h("span", { class: `cell lvl-${tone}`, "data-i": i }));
  });
  const summary = uptimeSummary(buckets, bucketSeconds, roundSeconds);
  strip.setAttribute("role", "img");
  strip.setAttribute("aria-label", summary);
  $("#uptime").replaceChildren(strip);
  $("#uptime-summary").textContent = summary;
  bindStripTooltip(strip, buckets, bucketSeconds, roundSeconds);
}

function uptimeSummary(buckets, bucketSeconds, roundSeconds) {
  const total = (key) => buckets.reduce((sum, b) => sum + b[key], 0);
  const watched = total("rounds") * roundSeconds;
  if (!watched) return "No connection data yet. It fills in as sitrep runs.";
  const dark = total("dark") * roundSeconds;
  const partial = total("partial") * roundSeconds;
  const fullDay = buckets.length * bucketSeconds;
  const coverage = watched < fullDay * 0.95 ? ` (sitrep has been watching for about ${duration(watched)})` : "";
  if (!dark && !partial) return `No problems in the last 24 hours${coverage}.`;
  const parts = [];
  if (dark) parts.push(`internet unreachable for about ${duration(dark)}`);
  if (partial) parts.push(`some checks failing for about ${duration(partial)}`);
  const text = parts.join("; ");
  return `${text[0].toUpperCase()}${text.slice(1)}${coverage}.`;
}

function bindStripTooltip(strip, buckets, bucketSeconds, roundSeconds) {
  const tip = $("#strip-tip");
  const card = $("#strip-card");
  let active = null;
  const hide = () => {
    tip.hidden = true;
    active?.classList.remove("active");
    active = null;
  };
  const show = (event) => {
    const cell = event.target.closest(".cell");
    if (!cell) return hide();
    const bucket = buckets[Number(cell.dataset.i)];
    let line = "No data";
    if (bucket.rounds) {
      if (bucket.dark) line = `Internet unreachable for about ${duration(bucket.dark * roundSeconds)}`;
      else if (bucket.partial) line = `Some checks failing for about ${duration(bucket.partial * roundSeconds)}`;
      else line = "All checks OK";
    }
    tip.replaceChildren(
      h("strong", { text: `${clockTime(bucket.start)} – ${clockTime(bucket.start + bucketSeconds)}` }), line);
    active?.classList.remove("active");
    active = cell;
    cell.classList.add("active");
    const cardBox = card.getBoundingClientRect();
    const cellBox = cell.getBoundingClientRect();
    tip.hidden = false;
    const half = tip.offsetWidth / 2 + 4;
    const centre = cellBox.left + cellBox.width / 2 - cardBox.left;
    tip.style.left = `${Math.min(Math.max(centre, half), cardBox.width - half)}px`;
    tip.style.top = `${cellBox.top - cardBox.top}px`;
  };
  strip.addEventListener("pointermove", show);
  strip.addEventListener("pointerdown", show);
  strip.addEventListener("pointerleave", hide);
}

function renderTimeline() {
  const list = $("#timeline");
  const events = (app.timeline && app.timeline.events) || [];
  if (!events.length) {
    list.replaceChildren(h("li", { class: "empty", text: "Nothing has changed in the last 24 hours." }));
    return;
  }
  const names = Object.fromEntries((app.state?.sources || []).map((s) => [s.id, s.name]));
  list.replaceChildren(...events.map((event) => {
    const details = event.details || {};
    const isOverall = event.source_id === "overall";
    const info = (isOverall ? OVERALL : LEVELS)[event.level] || LEVELS.unknown;
    const isChange = "from" in details;
    const title = isChange ? (isOverall ? "Overall" : names[event.source_id] || event.source_id) : event.title;
    const link = safeUrl(event.url);
    return h("li", { class: `event lvl-${event.level}` },
      h("time", { class: "event-time", datetime: new Date(event.ts * 1000).toISOString(), text: clockTime(event.ts) }),
      h("span", { class: "event-dot" }, icon(info.icon, 14)),
      h("div", {},
        h("p", { class: "event-title" }, h("strong", { text: title }),
          isChange ? h("span", { class: "to", text: ` → ${info.label}` }) : null),
        details.reason ? h("p", { class: "event-reason", text: sentence(details.reason) }) : null,
        link ? h("p", { class: "event-reason" }, h("a", { href: link, target: "_blank", rel: "noopener noreferrer", text: "Open ↗" })) : null));
  }));
}

function renderFooter() {
  const live = app.state.sources.filter((s) => ["normal", "elevated", "critical"].includes(s.level)).length;
  const lines = [
    `sitrep v${app.state.version} · ${live} of ${app.state.sources.length} sources live · ` +
    `checked ${timeFormat.format(new Date(app.lastContact || Date.now()))}`,
    ...(app.state.alerts || []).map(alertStatus),
  ];
  $("#footer").replaceChildren(...lines.map((text) => h("p", { text })));
}

function alertStatus(a) {
  if (a.state === "off") return `${a.name} alerts off. ${sentence(a.detail)}`;
  const parts = [a.state === "problem" ? `${a.name} alerts aren't working. ${sentence(a.detail)}`
    : sentence(`${a.name} alerts on${a.detail ? `, ${a.detail}` : ""}`)];
  if (a.waiting) parts.push("An alert is waiting to be sent.");
  if (a.last_sent_at) parts.push(`Last alert sent ${clockTime(a.last_sent_at)}.`);
  return parts.join(" ");
}

function updateTitleAndFavicon(level) {
  const info = OVERALL[level] || OVERALL.unknown;
  document.title = (app.state?.simulation ? "SIMULATION · " : "") +
    (level === "normal" ? "sitrep · All clear" : `sitrep · ${info.label}`);
  if (app.faviconLevel === level) return;
  app.faviconLevel = level;
  const colour = FAVICON_COLOURS[level] || FAVICON_COLOURS.unknown;
  const svg =
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32"><rect width="32" height="32" rx="8" fill="#1a1a19"/>' +
    `<circle cx="16" cy="16" r="9" fill="none" stroke="${colour}" stroke-width="2.5"/>` +
    `<circle cx="16" cy="16" r="4.5" fill="${colour}"/></svg>`;
  $("#favicon").href = `data:image/svg+xml,${encodeURIComponent(svg)}`;
}

// --- Start ---

bindNetworkTooltip();
bindRadarTooltip();
bindGridTooltip();
bindBlueskyTooltip();
render();
refreshState();
refreshSlow();
setInterval(refreshState, STATE_EVERY_MS);
setInterval(refreshSlow, SLOW_EVERY_MS);
setInterval(render, TICK_EVERY_MS);
setInterval(() => {
  if (!app.state?.simulation) return;
  refreshState();
  refreshSlow();
}, SIMULATION_EVERY_MS);
document.addEventListener("visibilitychange", () => {
  if (!document.hidden) {
    refreshState();
    refreshSlow();
  }
});
