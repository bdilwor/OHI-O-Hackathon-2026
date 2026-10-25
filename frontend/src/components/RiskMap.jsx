import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import DeckGL from '@deck.gl/react';
import { AmbientLight, DirectionalLight, FlyToInterpolator, LightingEffect } from '@deck.gl/core';
import { LineLayer, PathLayer, PolygonLayer, ScatterplotLayer, TextLayer } from '@deck.gl/layers';
import { SimpleMeshLayer } from '@deck.gl/mesh-layers';
import Map from 'react-map-gl/maplibre';

import { BASEMAP } from '../basemap.js';
import { outageColor, pct, riskColor, shade } from '../format.js';
import { MESHES } from '../meshes.js';
import { colorTrees, landuseKinds, sampleTrees } from '../trees.js';

const OVERVIEW = { longitude: -79.96, latitude: 37.27, zoom: 10.3, pitch: 0, bearing: 0 };
const CAMERA = { '3d': { zoom: 16.9, pitch: 60, bearing: -28 }, '2d': { zoom: 15.6, pitch: 0, bearing: 0 } };
const PATH_COLORS = { normal: [14, 165, 233], outage: [240, 82, 82], restored: [34, 197, 94] };
const PATH_Z = 14;            // supply path floats above roofs and wires in 3D
const OUTAGE_P = 0.5;         // a home at >= 50% is shown as a predicted outage
const HOUSE = [1.7, 1.7, 1.7];
const YOUR_HOUSE = [2.5, 2.5, 2.5];
const GEAR = [1.6, 1.6, 1.4];
const TREE = 1.3;
const PARTICLES = 7;
const RAIN_DROPS = 420;
const FPS = 20;
const M_PER_DEG = 111320;
const ON_TOP = { depthCompare: 'always' };
const LIGHTS = new LightingEffect({
  ambient: new AmbientLight({ color: [255, 255, 255], intensity: 1.15 }),
  sun: new DirectionalLight({ color: [255, 240, 214], intensity: 1.6, direction: [-2, -3, -4] }),
  fill: new DirectionalLight({ color: [190, 210, 255], intensity: 0.5, direction: [3, 2, -1] }),
});
const SHORT = {
  Distribution: 'Transformer', Fuse: 'Fuse', 'Recloser (Hydraulic)': 'Recloser', 'Recloser (Electronic)': 'Recloser',
  Sectionalizer: 'Sectionalizer', Step: 'Step transformer', home: 'Your home',
};

export const LAYERS = [
  { id: 'homes', label: 'Homes', swatch: '#22c55e' },
  { id: 'grid', label: 'Poles, equipment & wires', swatch: '#f59e0b' },
  { id: 'trees', label: 'Trees (3D)', swatch: '#15803d' },
  { id: 'path', label: 'Your supply path', swatch: '#0ea5e9' },
];

const hash = (a, b = 0) => { const s = Math.sin(a * 12.9898 + b * 78.233) * 43758.5453; return s - Math.floor(s); };

/** Brighter, wider roads and a sky/horizon on the basemap. */
function styleBasemap(map) {
  for (const layer of map.getStyle().layers) {
    if (layer.type !== 'line' || layer['source-layer'] !== 'roads') continue;
    const w = map.getPaintProperty(layer.id, 'line-width');
    let wider = null;
    if (typeof w === 'number') wider = w * 1.5;
    else if (w?.stops) wider = { ...w, stops: w.stops.map(([z, v]) => [z, v * 1.5]) }; // legacy zoom function
    else if (Array.isArray(w)) wider = ['*', 1.5, w];                                   // expression
    if (wider !== null) map.setPaintProperty(layer.id, 'line-width', wider);
  }
  try {
    map.setSky({ 'sky-color': '#7cc3f5', 'horizon-color': '#e0f2fe', 'fog-color': '#f1f5f9',
                 'sky-horizon-blend': 0.6, 'horizon-fog-blend': 0.6, 'fog-ground-blend': 0.85 });
  } catch { /* older style spec */ }
}

/** Overhead spans sag between support points (a few metres per span), which reads as real wire. */
function sagPath(path, lat) {
  if (!path.length || !path[0][2]) return path;
  const mLon = M_PER_DEG * Math.cos((lat * Math.PI) / 180);
  const out = [path[0]];
  for (let i = 1; i < path.length; i += 1) {
    const [a, b] = [path[i - 1], path[i]];
    const len = Math.hypot((b[0] - a[0]) * mLon, (b[1] - a[1]) * M_PER_DEG);
    const sag = Math.min(1.6, len * 0.03);
    for (let k = 1; k <= 4; k += 1) {
      const t = k / 5;
      out.push([a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t, a[2] + (b[2] - a[2]) * t - sag * 4 * t * (1 - t)]);
    }
    out.push(b);
  }
  return out;
}

function flowPoints(path, phase) {
  const seg = path.slice(1).map((p, i) => Math.hypot(p[0] - path[i][0], p[1] - path[i][1]));
  const total = seg.reduce((a, b) => a + b, 0);
  if (!total) return [];
  return Array.from({ length: PARTICLES }, (_, k) => {
    let d = (((k / PARTICLES) + phase) % 1) * total;
    let i = 0;
    while (i < seg.length - 1 && d > seg[i]) { d -= seg[i]; i += 1; }
    const t = seg[i] ? d / seg[i] : 0;
    return { position: [path[i][0] + (path[i + 1][0] - path[i][0]) * t,
      path[i][1] + (path[i + 1][1] - path[i][1]) * t, path[i][2] ?? 0] };
  });
}

function useAnimationTime(active) {
  const [time, setTime] = useState(0);
  useEffect(() => {
    if (!active) return undefined;
    let raf;
    let last = 0;
    const start = performance.now();
    const tick = (now) => {
      if (now - last > 1000 / FPS) { last = now; setTime((now - start) / 1000); }
      raf = requestAnimationFrame(tick);
    };
    raf = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(raf);
  }, [active]);
  return time;
}

const NOTE = '<span style="color:#64748b">Model estimate for the shown weather.</span>';
const TOOLTIPS = {
  homes: (o) => (o.is_you
    ? `<b>Your home</b><br/>Chance of an outage: <b>${pct(o.outage_p, 0)}</b><br/>${NOTE}`
    : `<b>A home near you</b><br/>Chance of an outage in this area: <b>${pct(o.outage_p, 1)}</b>
       ${o.outage_p >= OUTAGE_P ? '<br/><b style="color:#dc2626">Outage likely</b>' : ''}<br/>${NOTE}`),
  poles: (o) => `<b>Utility pole</b><br/>Chance it's damaged: <b>${pct(o.p, 2)}</b><br/>${NOTE}`,
  equipment: (o) => `<b>${o.kind}</b><br/>Chance it's damaged: <b>${pct(o.p, 2)}</b><br/>${NOTE}`,
  lines: (o) => `<b>${o.main_line ? 'Main power line' : o.kind === 'secondary' ? 'Street-level wire' : 'Branch line'}</b>
    ${o.overhead ? '' : ' (underground)'}<br/>Chance this span is damaged: <b>${pct(o.p, 2)}</b><br/>${NOTE}`,
  trees: (o) => `<b>${o.kind === 'pine' ? 'Pine' : 'Tree'}</b> (from OpenStreetMap land use)<br/>
    Outage chance nearby: <b>${pct(o.outage_p, 1)}</b>`,
  'path-nodes': (o) => (o.kind === 'home' ? '<b>Your home</b>'
    : `<b>${o.label}</b><br/>Chance it's damaged: <b>${pct(o.p, 1)}</b><br/>
       <span style="color:#64748b">On your supply path. Crews restore main-line devices first.</span>`),
  footprint: (o) => `<b>Simulated storm area</b><br/>${Math.round(o.radius)} m around your home`,
  'outage-area': (o) => `<b>Outage area</b><br/>~${o.homes} homes without power`,
  'outage-fault': (o) => `<b>Fault</b>: ${o.cause}<br/>${o.location}`,
};

function tooltip({ object, layer }) {
  if (!object || !layer) return null;
  const fn = TOOLTIPS[layer.id.split('--')[0]];
  return fn ? { html: fn(object), className: 'deck-tooltip' } : null;
}

const key = (p) => `${p[0].toFixed(5)},${p[1].toFixed(5)}`;
const RED = [220, 38, 38];

export default function RiskMap({ data, viewMode = '3d', layersOn, pathState = 'normal', bottleneck, onMapClick,
                                  wind = 0, footprint = null, outage = null, basemap = BASEMAP, active = true }) {
  const [viewState, setViewState] = useState(OVERVIEW);
  const [treeSpots, setTreeSpots] = useState([]);
  const mapRef = useRef(null);
  const treesFor = useRef(null);
  const time = useAnimationTime(Boolean(data) && active);  // hidden maps don't animate
  const is3d = viewMode === '3d';
  const homeKey = data?.home?.join(',');
  const lat = data?.home?.[1] ?? 37.2;

  useEffect(() => {
    if (!data?.home) return;
    setViewState((vs) => ({
      ...vs, longitude: data.home[0], latitude: data.home[1], ...CAMERA[viewMode],
      transitionDuration: 1800, transitionInterpolator: new FlyToInterpolator({ speed: 1.6 }),
    }));
  }, [homeKey, viewMode]); // eslint-disable-line react-hooks/exhaustive-deps

  // ---- trees from OSM land use, re-sampled while tiles load ----
  useEffect(() => { treesFor.current = null; setTreeSpots([]); }, [homeKey]);
  const loadTrees = useCallback(() => {
    const map = mapRef.current?.getMap?.();
    if (!map || !data?.home || treesFor.current === homeKey) return;
    const spots = sampleTrees(map, data.home, data.radius_m, data.homes);
    if (import.meta.env.DEV && !spots.length) { // basemap schema check when no land use is found
      const src = Object.keys(map.getStyle().sources)[0];
      console.info('landuse kinds near home:', landuseKinds(map.querySourceFeatures(src, { sourceLayer: 'landuse' })));
    }
    setTreeSpots((cur) => (spots.length > cur.length ? spots : cur));
    if (spots.length && map.getZoom() > 15.5) treesFor.current = homeKey;
  }, [data, homeKey]);
  const trees = useMemo(() => colorTrees(treeSpots, data?.homes), [treeSpots, data?.homes]);
  const pines = useMemo(() => trees.filter((t) => t.kind === 'pine'), [trees]);
  const leafy = useMemo(() => trees.filter((t) => t.kind !== 'pine'), [trees]);

  // ---- static geometry derived from the payload ----
  const live = outage && outage.status !== 'restored' ? outage : null;
  const affected = useMemo(() => new Set((live?.affected?.positions || []).map(key)), [live]);
  const homes = useMemo(() => (data?.homes || []).map((h, i) => ({
    ...h, out: affected.has(key(h.position)) || (h.is_you && live?.affects_you), yaw: Math.round(hash(i, 1) * 4) * 90 + (hash(i, 2) - 0.5) * 20,
    scale: h.is_you ? YOUR_HOUSE : [HOUSE[0] * (0.85 + hash(i, 3) * 0.35), HOUSE[1] * (0.9 + hash(i, 4) * 0.25), HOUSE[2]],
  })), [data?.homes, affected, live]);
  const outageArea = useMemo(() => (live?.affected?.polygon?.length
    ? [{ polygon: live.affected.polygon, homes: live.affected.homes }] : []), [live]);
  const fault = useMemo(() => (live ? [{ position: live.fault_position, cause: live.cause, location: live.location }] : []),
    [live]);
  const lines3d = useMemo(() => (data?.lines || []).map((l) => ({ ...l, path: sagPath(l.path, lat) })), [data, lat]);
  const lines2d = useMemo(() => (data?.lines || []).map((l) => ({ ...l, path: l.path.map(([x, y]) => [x, y]) })), [data]);
  const hotLines = useMemo(() => lines3d.filter((l) => l.p >= 0.03 && l.overhead), [lines3d]);
  const supply = useMemo(() => {
    const nodes = data?.supply_path || [];
    const z = is3d ? PATH_Z : 0;
    return nodes.length < 2 ? null : { path: [...nodes].reverse().map((n) => [...n.position, z]) };
  }, [data, is3d]);
  const rainSeeds = useMemo(() => Array.from({ length: RAIN_DROPS }, (_, i) => [hash(i, 7), hash(i, 8), hash(i, 9)]), []);
  const transformers = useMemo(() => (data?.equipment || []).filter((e) => e.kind === 'Transformer'), [data]);
  const devices = useMemo(() => (data?.equipment || []).filter((e) => e.kind !== 'Transformer'), [data]);
  const outHomes = useMemo(() => homes.filter((h) => h.out || h.outage_p >= OUTAGE_P), [homes]);
  const footprintData = useMemo(() => (data?.home && footprint ? [{ position: data.home, radius: footprint }] : []),
    [data?.home, footprint]);
  const supplyData = useMemo(() => (supply ? [supply] : []), [supply]);

  // ---- animation-dependent pieces ----
  const flow = supply ? flowPoints(supply.path, (time * 0.18) % 1) : [];
  const lean = 2 + wind * 14;                        // trees lean with the wind (static: no per-frame updates)
  const pulse = 0.5 + 0.5 * Math.sin(time * 4);      // drives layer-wide opacity/scale uniforms only
  const rainTick = Math.floor(time * 15);
  const rain = useMemo(() => {
    if (!data?.home || wind < 0.35 || !is3d) return [];
    const n = Math.round(RAIN_DROPS * Math.min(1, (wind - 0.25) * 1.4));
    const mLon = M_PER_DEG * Math.cos((lat * Math.PI) / 180);
    const slant = 6 + wind * 14;
    return rainSeeds.slice(0, n).map(([a, b, c]) => {
      const z = 80 - ((c * 80 + (rainTick / 15) * 55) % 80);
      const x = data.home[0] + ((a - 0.5) * 1100 + slant * (80 - z) / 8) / mLon;
      const y = data.home[1] + (b - 0.5) * 1100 / M_PER_DEG;
      return { from: [x, y, z + 5], to: [x + slant / mLon, y, z] };
    });
  }, [data, wind, is3d, rainTick, rainSeeds, lat]);

  const pathColor = PATH_COLORS[pathState] || PATH_COLORS.normal;
  const bottleneckLabel = bottleneck?.toLowerCase();
  const isBottleneck = (d) => d.label?.toLowerCase() === bottleneckLabel;
  const trig = data?.storm?.storm_n;
  const fade = { getColor: 700 };
  const treeLayer = (id, items, mesh, colorFn, pickable) => new SimpleMeshLayer({
    id, data: items, mesh, pickable, getPosition: (d) => d.position,
    getScale: (d) => [d.scale * TREE, d.scale * TREE, d.scale * TREE],
    getOrientation: (d) => [lean * (0.7 + 0.3 * Math.sin(d.phase)), d.yaw, lean * 0.3 * Math.cos(d.phase)],
    getColor: colorFn, updateTriggers: { getOrientation: lean, getColor: trig }, transitions: fade,
  });
  const leafColor = (d) => shade(outageColor(d.outage_p).map((v, k) => v * 0.55 + [40, 120, 60][k] * 0.45), d.tint);

  const layers = !data ? [] : [
    // ---- sandbox storm footprint ----
    footprint && new ScatterplotLayer({
      id: 'footprint', data: footprintData, pickable: !is3d, radiusUnits: 'meters',
      getPosition: (d) => d.position, getRadius: (d) => d.radius, filled: true, stroked: true,
      getFillColor: [124, 58, 237, 40], getLineColor: [124, 58, 237, 220], lineWidthUnits: 'pixels',
      getLineWidth: 2.5, opacity: 0.6 + 0.4 * pulse,
    }),
    // ---- trees (3D only) ----
    is3d && layersOn.trees && treeLayer('trees--pine-trunk', pines, MESHES.pineTrunk, [255, 255, 255], false),
    is3d && layersOn.trees && treeLayer('trees--pine', pines, MESHES.pine, leafColor, true),
    is3d && layersOn.trees && treeLayer('trees--leafy-trunk', leafy, MESHES.leafyTrunk, [255, 255, 255], false),
    is3d && layersOn.trees && treeLayer('trees--leafy', leafy, MESHES.leafy, leafColor, true),
    // ---- grid ----
    layersOn.grid && is3d && new PathLayer({
      id: 'lines-glow', data: hotLines, getPath: (d) => d.path, widthUnits: 'pixels', getWidth: 7,
      getColor: (d) => [...riskColor(d.p), 110], updateTriggers: { getColor: trig }, opacity: 0.35 + 0.65 * pulse,
    }),
    layersOn.grid && new PathLayer({
      id: 'lines', data: is3d ? lines3d : lines2d, pickable: true, getPath: (d) => d.path, widthUnits: 'pixels',
      getWidth: (d) => (d.main_line ? 3.4 : d.kind === 'secondary' ? 1.2 : 2),
      getColor: (d) => [...shade(riskColor(d.p), 0.85), d.overhead ? 255 : 120],
      updateTriggers: { getColor: trig, getPath: is3d }, transitions: fade,
    }),
    layersOn.grid && is3d && new SimpleMeshLayer({
      id: 'poles--3d', data: data.poles, mesh: MESHES.pole, pickable: true, getPosition: (d) => d.position,
      getScale: [1.5, 1.5, 1.3], getColor: (d) => shade(riskColor(d.p), 0.8), updateTriggers: { getColor: trig },
      transitions: fade,
    }),
    layersOn.grid && is3d && new SimpleMeshLayer({
      id: 'poles--insulators', data: data.poles, mesh: MESHES.insulators, getPosition: (d) => d.position,
      getScale: [1.5, 1.5, 1.3], getColor: [236, 240, 245],
    }),
    layersOn.grid && is3d && new SimpleMeshLayer({
      id: 'equipment--transformer', data: transformers,
      mesh: MESHES.transformer, pickable: true, getPosition: (d) => d.position, getScale: GEAR,
      getColor: (d) => [148, 163, 184].map((v, k) => v * 0.6 + riskColor(d.p)[k] * 0.4),
      updateTriggers: { getColor: trig }, transitions: fade,
    }),
    layersOn.grid && is3d && new SimpleMeshLayer({
      id: 'equipment--device', data: devices, mesh: MESHES.device,
      pickable: true, getPosition: (d) => d.position, getScale: GEAR, getColor: (d) => riskColor(d.p),
      updateTriggers: { getColor: trig }, transitions: fade,
    }),
    layersOn.grid && !is3d && new ScatterplotLayer({
      id: 'poles--2d', data: data.poles, pickable: true, getPosition: (d) => d.position, radiusUnits: 'pixels',
      getRadius: 2.4, getFillColor: (d) => riskColor(d.p), updateTriggers: { getFillColor: trig },
    }),
    layersOn.grid && !is3d && new ScatterplotLayer({
      id: 'equipment--2d', data: data.equipment, pickable: true, getPosition: (d) => d.position, radiusUnits: 'pixels',
      getRadius: 4.5, getFillColor: (d) => riskColor(d.p), stroked: true, getLineColor: [255, 255, 255],
      lineWidthUnits: 'pixels', getLineWidth: 1.2, updateTriggers: { getFillColor: trig },
    }),
    // ---- homes ----
    layersOn.homes && is3d && new SimpleMeshLayer({
      id: 'homes--3d', data: homes, mesh: MESHES.house, pickable: true, getPosition: (d) => d.position,
      getOrientation: (d) => [0, d.yaw, 0], getScale: (d) => d.scale,
      getColor: (d) => (d.out ? RED : outageColor(d.outage_p)), updateTriggers: { getColor: [trig, affected] },
      transitions: fade,
    }),
    layersOn.homes && is3d && new SimpleMeshLayer({
      id: 'homes--windows', data: homes, mesh: MESHES.windows, getPosition: (d) => d.position,
      getOrientation: (d) => [0, d.yaw, 0], getScale: (d) => d.scale,
      getColor: (d) => (d.out || d.outage_p >= OUTAGE_P ? [45, 52, 64] : [255, 214, 120]),
      updateTriggers: { getColor: [trig, affected] },
    }),
    layersOn.homes && new ScatterplotLayer({
      id: 'homes--out', data: outHomes, radiusUnits: 'meters', stroked: true,
      filled: false, getPosition: (d) => d.position, getRadius: 10, radiusScale: 0.9 + 0.7 * pulse,
      lineWidthUnits: 'pixels', getLineWidth: 2, getLineColor: [220, 38, 38, 230], opacity: 1 - 0.6 * pulse,
    }),
    layersOn.homes && !is3d && new ScatterplotLayer({
      id: 'homes--2d', data: homes, pickable: true, getPosition: (d) => d.position, radiusUnits: 'meters',
      radiusMinPixels: 3, getRadius: (d) => (d.is_you ? 12 : 7), getFillColor: (d) => (d.out ? RED : outageColor(d.outage_p)),
      stroked: true, getLineColor: (d) => (d.is_you ? [15, 23, 42] : [255, 255, 255]), lineWidthUnits: 'pixels',
      getLineWidth: (d) => (d.is_you ? 2.5 : 1), updateTriggers: { getFillColor: [trig, affected] },
    }),
    // ---- current outage: affected area + fault ----
    outageArea.length > 0 && new PolygonLayer({
      id: 'outage-area', data: outageArea, pickable: true, getPolygon: (d) => d.polygon, stroked: true, filled: true,
      getFillColor: [239, 68, 68, 45], getLineColor: [220, 38, 38, 230], lineWidthUnits: 'pixels', getLineWidth: 2.5,
      opacity: 0.55 + 0.45 * pulse,
    }),
    fault.length > 0 && new ScatterplotLayer({
      id: 'outage-fault', data: fault, pickable: true, parameters: ON_TOP, radiusUnits: 'pixels',
      getPosition: (d) => [...d.position, is3d ? PATH_Z : 0], getRadius: 9, radiusScale: 0.9 + 0.5 * pulse,
      getFillColor: RED, stroked: true, getLineColor: [255, 255, 255], lineWidthUnits: 'pixels', getLineWidth: 3,
      updateTriggers: { getPosition: is3d },
    }),
    fault.length > 0 && new TextLayer({
      id: 'outage-fault-label', data: fault, parameters: ON_TOP, getPosition: (d) => [...d.position, is3d ? PATH_Z : 0],
      getText: (d) => `Fault: ${d.cause}`, getSize: 13, getColor: [255, 255, 255], getPixelOffset: [0, 26],
      fontFamily: 'Inter, system-ui, sans-serif', fontWeight: 700, background: true, getBackgroundColor: [185, 28, 28, 235],
      backgroundPadding: [6, 4], characterSet: 'auto', updateTriggers: { getPosition: is3d },
    }),
    // ---- weather ----
    rain.length > 0 && new LineLayer({
      id: 'rain', data: rain, getSourcePosition: (d) => d.from, getTargetPosition: (d) => d.to,
      getColor: [96, 165, 250, 150], widthUnits: 'pixels', getWidth: 1.2,
    }),
    // ---- your supply path ----
    layersOn.path && supply && new PathLayer({
      id: 'path-glow', data: supplyData, getPath: (d) => d.path, widthUnits: 'pixels', getWidth: 11,
      getColor: [...pathColor, 70], capRounded: true, jointRounded: true, parameters: ON_TOP,
    }),
    layersOn.path && supply && new PathLayer({
      id: 'path-core', data: supplyData, getPath: (d) => d.path, widthUnits: 'pixels', getWidth: 3,
      getColor: [...pathColor, 240], capRounded: true, jointRounded: true, parameters: ON_TOP,
    }),
    layersOn.path && flow.length > 0 && new ScatterplotLayer({
      id: 'path-flow', data: flow, getPosition: (d) => d.position, radiusUnits: 'pixels', getRadius: 3.8,
      getFillColor: [255, 255, 255, 245], stroked: true, getLineColor: [...pathColor, 255], lineWidthUnits: 'pixels',
      getLineWidth: 2, parameters: ON_TOP,
    }),
    layersOn.path && new ScatterplotLayer({
      id: 'path-nodes', data: data.supply_path, pickable: true, radiusUnits: 'pixels', parameters: ON_TOP,
      getPosition: (d) => [...d.position, is3d ? PATH_Z : 0],
      getRadius: (d) => (d.kind === 'home' ? 8 : isBottleneck(d) ? 10 : 6),
      getFillColor: (d) => (d.kind === 'home' ? [255, 255, 255] : riskColor(d.p)),
      stroked: true, getLineColor: (d) => (isBottleneck(d) ? [250, 204, 21] : [15, 23, 42]), lineWidthUnits: 'pixels',
      getLineWidth: (d) => (isBottleneck(d) ? 3 : 2),
      updateTriggers: { getRadius: bottleneckLabel, getLineColor: bottleneckLabel, getLineWidth: bottleneckLabel,
                        getPosition: is3d, getFillColor: trig },
    }),
    layersOn.path && new TextLayer({
      id: 'path-labels', data: data.supply_path, parameters: ON_TOP,
      getPosition: (d) => [...d.position, is3d ? PATH_Z : 0],
      getText: (d) => (isBottleneck(d) ? `${SHORT[d.kind] || 'Device'} · likely bottleneck` : SHORT[d.kind] || 'Device'),
      getSize: 12, getColor: [255, 255, 255], fontFamily: 'Inter, system-ui, sans-serif', fontWeight: 600,
      getPixelOffset: (d, { index }) => (d.kind === 'home' ? [0, 22] : [0, index % 2 ? -16 : -48]),
      background: true, getBackgroundColor: [15, 23, 42, 215], backgroundPadding: [5, 3], characterSet: 'auto',
      updateTriggers: { getText: bottleneckLabel, getPosition: is3d },
    }),
  ].filter(Boolean);

  return (
    <DeckGL
      viewState={viewState}
      onViewStateChange={({ viewState: vs }) => setViewState(vs)}
      controller={{ dragRotate: true, touchRotate: true }}
      layers={layers}
      effects={[LIGHTS]}
      getTooltip={tooltip}
      onClick={(info) => onMapClick && info.coordinate && onMapClick(info.coordinate)}
      getCursor={({ isDragging, isHovering }) => (isDragging ? 'grabbing' : isHovering || onMapClick ? 'pointer' : 'grab')}
    >
      <Map ref={mapRef} mapStyle={basemap} reuseMaps attributionControl onIdle={loadTrees}
           onLoad={(e) => styleBasemap(e.target)} />
    </DeckGL>
  );
}
