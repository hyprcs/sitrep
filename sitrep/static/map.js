// sitrep map page: home and incidents over a Greater Sydney basemap, all served by sitrep itself.
// The basemap comes from `python -m sitrep map`; without it, the points still show on a plain background.
import { Map as MapLibreMap, NavigationControl, Popup, ScaleControl, addProtocol, setWorkerUrl } from "./vendor/maplibre-gl/maplibre-gl.mjs";

const REFRESH_MS = 60_000;
const BASE = location.href.replace(/[?#].*$/, "").replace(/[^/]*$/, ""); // this page's folder, for asset links
const COLOURS = { critical: "#d03b3b", elevated: "#fab219", watch: "#ec835a", info: "#898781" };
// The outline says where a point came from: fires dark, road hazards blue.
const OUTLINES = { rfs: "#0d0d0d", traffic: "#86b6ef", news: "#f0efec", story: "#f0efec", term: "#c3c2b7",
  convergence: "#fab219", aircraft: "#b58cf2" };
const ATTRIBUTION = '<a href="https://github.com/protomaps/basemaps">Protomaps</a> © ' +
  '<a href="https://openstreetmap.org/copyright">OpenStreetMap</a>';

const $ = (selector) => document.querySelector(selector);
const timeFormat = new Intl.DateTimeFormat(undefined, { weekday: "short", hour: "2-digit", minute: "2-digit" });
const dateFormat = new Intl.DateTimeFormat(undefined, { day: "numeric", month: "short", year: "numeric" });

setWorkerUrl(`${BASE}vendor/maplibre-gl/maplibre-gl-worker.mjs`);
addProtocol("pmtiles", new pmtiles.Protocol().tile);

async function getJSON(path) {
  const response = await fetch(path, { cache: "no-store" });
  if (!response.ok) throw new Error(`HTTP ${response.status}`);
  return response.json();
}

function el(tag, cls, text) {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text !== undefined) node.textContent = text; // never HTML: titles come from outside sources
  return node;
}

function baseStyle(basemap) {
  const style = {
    version: 8,
    glyphs: `${BASE}vendor/basemaps-assets/fonts/{fontstack}/{range}.pbf`,
    sources: {},
    layers: [{ id: "background", type: "background", paint: { "background-color": "#0d0d0d" } }],
  };
  if (basemap.present) {
    style.sprite = `${BASE}vendor/basemaps-assets/sprites/v4/dark`;
    style.sources.protomaps = { type: "vector", url: `pmtiles://${BASE}${basemap.url}`, attribution: ATTRIBUTION };
    style.layers = basemaps.layers("protomaps", basemaps.namedFlavor("dark"), { lang: "en" });
  }
  return style;
}

function pointsGeoJSON(data) {
  const order = { info: 0, watch: 1, elevated: 2, critical: 3 }; // worst drawn last, on top
  const features = [...data.points]
    .sort((a, b) => (order[a.mark] ?? 0) - (order[b.mark] ?? 0))
    .map((p) => ({
      type: "Feature",
      geometry: { type: "Point", coordinates: [p.lon, p.lat] },
      properties: {
        ...p, colour: COLOURS[p.mark] || COLOURS.info, outline: OUTLINES[p.kind] || "#0d0d0d",
        radius: p.kind === "convergence" ? 26
          : (p.mark === "critical" ? 9 : p.mark === "elevated" ? 8 : p.mark === "watch" ? 7 : 5) + (p.kind === "story" ? 4 : 0),
        opacity: p.kind === "convergence" ? 0.25 : 1,
      },
    }));
  return { type: "FeatureCollection", features };
}

function homeGeoJSON(data) {
  return { type: "FeatureCollection", features: [{ type: "Feature", geometry: { type: "Point", coordinates: [data.home.lon, data.home.lat] }, properties: { name: data.home.name } }] };
}

function addOverlays(map, data) {
  map.addSource("points", { type: "geojson", data: pointsGeoJSON(data) });
  map.addSource("home", { type: "geojson", data: homeGeoJSON(data) });
  map.addLayer({
    id: "points", type: "circle", source: "points",
    paint: {
      "circle-color": ["get", "colour"], "circle-radius": ["get", "radius"], "circle-opacity": ["get", "opacity"],
      "circle-stroke-color": ["get", "outline"], "circle-stroke-width": 2,
    },
  });
  map.addLayer({
    id: "home", type: "circle", source: "home",
    paint: { "circle-radius": 8, "circle-color": "rgba(0,0,0,0)", "circle-stroke-color": "#f0efec", "circle-stroke-width": 3 },
  });
  map.addLayer({
    id: "home-label", type: "symbol", source: "home",
    layout: { "text-field": "Home", "text-font": ["Noto Sans Medium"], "text-size": 12, "text-offset": [0, 1.3], "text-anchor": "top" },
    paint: { "text-color": "#f0efec", "text-halo-color": "#0d0d0d", "text-halo-width": 1.5 },
  });
}

function popupFor(props) {
  const box = el("div");
  box.append(el("p", "popup-title", props.title));
  const lines = [
    [props.source_name, props.label].filter(Boolean).join(": "),
    props.km != null ? `${Math.round(props.km)} km from home` : "",
    props.detail || "",
    props.at ? `Updated ${timeFormat.format(new Date(props.at * 1000))}` : "",
  ];
  for (const line of lines) if (line) box.append(el("p", "popup-line", line));
  if (props.live === false) box.append(el("p", "popup-line", "This source isn't up to date right now."));
  return box;
}

function showStatus(data, error) {
  if (error) {
    $("#map-status").textContent = "Can't reach the sitrep server; showing what it last said. Retrying every minute.";
    return;
  }
  const counts = {};
  for (const p of data.points) counts[p.mark] = (counts[p.mark] || 0) + 1;
  const near = (counts.critical || 0) + (counts.elevated || 0);
  $("#map-status").textContent = `${data.points.length} points on the map` +
    (near ? `, ${near} that matter near home` : ", none that matter near home") +
    ` · updated ${timeFormat.format(new Date(data.server_time * 1000))}`;
  $("#legend-counts").textContent = data.basemap.present && data.basemap.made_at
    ? `Basemap from ${dateFormat.format(new Date(data.basemap.made_at * 1000))}.` : "";
  const note = $("#map-note");
  note.hidden = data.basemap.present;
  if (!data.basemap.present) {
    note.replaceChildren("No basemap yet, so the points are on a plain background. To add streets and suburbs, run ",
      el("code", "", "python -m sitrep map"), " once (about 40 MB), then reload this page.");
  }
}

async function start() {
  let data;
  try {
    data = await getJSON("api/map");
  } catch {
    $("#map-status").textContent = "Can't reach the sitrep server. Reload to try again.";
    return;
  }
  const [west, south, east, north] = data.bbox;
  const map = new MapLibreMap({
    container: "map",
    style: baseStyle(data.basemap),
    center: [data.home.lon, data.home.lat],
    zoom: 10,
    minZoom: 6,
    maxZoom: 16,
    maxBounds: [[west - 1, south - 1], [east + 1, north + 1]],
    attributionControl: { compact: true },
  });
  map.addControl(new NavigationControl({ showCompass: false }), "top-right");
  map.addControl(new ScaleControl({ unit: "metric" }), "bottom-right");
  map.on("load", () => {
    addOverlays(map, data);
    showStatus(data);
  });
  map.on("click", "points", (e) => {
    const feature = e.features[0];
    new Popup({ closeButton: true, maxWidth: "300px" })
      .setLngLat(feature.geometry.coordinates).setDOMContent(popupFor(feature.properties)).addTo(map);
  });
  map.on("mouseenter", "points", () => { map.getCanvas().style.cursor = "pointer"; });
  map.on("mouseleave", "points", () => { map.getCanvas().style.cursor = ""; });

  setInterval(async () => {
    try {
      data = await getJSON("api/map");
      map.getSource("points")?.setData(pointsGeoJSON(data));
      showStatus(data);
    } catch {
      showStatus(data, true);
    }
  }, REFRESH_MS);
}

start();
