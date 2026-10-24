// Tree positions from REAL OpenStreetMap land use in the basemap's vector tiles. The challenge data has no
// vegetation, so trees are placed on jittered grids: dense inside OSM wood/forest/park polygons, and sparse
// "yard trees" inside OSM residential areas (kept clear of houses). Woods follow real mapped areas; the
// exact yard-tree spots are illustrative. Trees are colored by the outage chance of the nearest home.

const WOODED = /wood|forest|park|nature|scrub|orchard|garden|cemetery|recreation/i;
const RESIDENTIAL = /residential|suburb|neighbourhood|allotments|village_green|grass|meadow/i;
const SPACING_M = { woods: 18, yard: 38 };
const PINE_SHARE = { woods: 0.6, yard: 0.25 };
const HOUSE_CLEARANCE_M = 14;
const MAX_TREES = 3000;
const M_PER_DEG_LAT = 111320;

function pointInRing(x, y, ring) {
  let inside = false;
  for (let i = 0, j = ring.length - 1; i < ring.length; j = i, i += 1) {
    const [xi, yi] = ring[i];
    const [xj, yj] = ring[j];
    if (yi > y !== yj > y && x < ((xj - xi) * (y - yi)) / (yj - yi) + xi) inside = !inside;
  }
  return inside;
}

const inPolygon = (x, y, rings) => pointInRing(x, y, rings[0]) && !rings.slice(1).some((h) => pointInRing(x, y, h));

function polygons(geometry) {
  if (geometry.type === 'Polygon') return [geometry.coordinates];
  if (geometry.type === 'MultiPolygon') return geometry.coordinates;
  return [];
}

// Deterministic jitter so trees don't move between renders.
const jitter = (i, j, k) => {
  const s = Math.sin(i * 127.1 + j * 311.7 + k * 74.7) * 43758.5453;
  return s - Math.floor(s) - 0.5;
};

/** Distinct landuse kinds near the home (debugging aid for the basemap schema). */
export function landuseKinds(features) {
  return [...new Set(features.map((f) => f.properties?.kind || f.properties?.class || f.properties?.['pmap:kind']))];
}

const kindOf = (f) => String(f.properties?.kind || f.properties?.class || f.properties?.['pmap:kind'] || '');

/** Tree spots within radiusM of home from OSM land use (call once the basemap is idle). */
export function sampleTrees(map, home, radiusM, homes = []) {
  if (!map || !home) return [];
  const style = map.getStyle();
  const source = Object.keys(style.sources).find((k) => style.sources[k].type === 'vector');
  if (!source) return [];
  const landuse = map.querySourceFeatures(source, { sourceLayer: 'landuse' });
  const mPerDegLon = M_PER_DEG_LAT * Math.cos((home[1] * Math.PI) / 180);
  const r2 = radiusM * radiusM;
  const clear2 = HOUSE_CLEARANCE_M * HOUSE_CLEARANCE_M;
  const nearHouse = (x, y) => homes.some((h) => {
    const dx = (h.position[0] - x) * mPerDegLon;
    const dy = (h.position[1] - y) * M_PER_DEG_LAT;
    return dx * dx + dy * dy < clear2;
  });
  const trees = [];
  for (const [zone, test] of [['woods', WOODED], ['yard', RESIDENTIAL]]) {
    const placed = sampleZone(landuse.filter((f) => test.test(kindOf(f))), zone, home, mPerDegLon, r2,
                              zone === 'yard' ? nearHouse : null, MAX_TREES - trees.length);
    trees.push(...placed);
  }
  return trees;
}

function sampleZone(feats, zone, home, mPerDegLon, r2, reject, limit) {
  const dLat = SPACING_M[zone] / M_PER_DEG_LAT;
  const dLon = SPACING_M[zone] / mPerDegLon;
  const seen = new Set();
  const trees = [];
  for (const f of feats) {
    for (const rings of polygons(f.geometry)) {
      const xs = rings[0].map((c) => c[0]);
      const ys = rings[0].map((c) => c[1]);
      const [x0, x1, y0, y1] = [Math.min(...xs), Math.max(...xs), Math.min(...ys), Math.max(...ys)];
      for (let i = Math.floor(x0 / dLon); i <= Math.ceil(x1 / dLon); i += 1) {
        for (let j = Math.floor(y0 / dLat); j <= Math.ceil(y1 / dLat); j += 1) {
          const key = `${i},${j}`;
          if (seen.has(key)) continue; // tiles split polygons; one tree per grid cell
          const x = (i + 0.5 + jitter(i, j, 1) * 0.7) * dLon;
          const y = (j + 0.5 + jitter(i, j, 2) * 0.7) * dLat;
          const dx = (x - home[0]) * mPerDegLon;
          const dy = (y - home[1]) * M_PER_DEG_LAT;
          if (dx * dx + dy * dy > r2 || !inPolygon(x, y, rings) || (reject && reject(x, y))) continue;
          seen.add(key);
          const r = jitter(i, j, 3) + 0.5;
          trees.push({
            position: [x, y], scale: 0.75 + r * 0.6, yaw: (jitter(i, j, 4) + 0.5) * 360,
            kind: jitter(i, j, 5) + 0.5 < PINE_SHARE[zone] ? 'pine' : 'leafy', tint: 0.85 + r * 0.3,
            phase: (jitter(i, j, 6) + 0.5) * Math.PI * 2,
          });
          if (trees.length >= limit) return trees;
        }
      }
    }
  }
  return trees;
}

/** Each tree takes the outage chance of its nearest home (recomputed when the storm changes). */
export function colorTrees(trees, homes) {
  if (!trees.length) return trees;
  const mPerDegLon = M_PER_DEG_LAT * Math.cos((trees[0].position[1] * Math.PI) / 180);
  if (!homes?.length) return trees.map((t) => ({ ...t, outage_p: 0 }));
  return trees.map((t) => {
    let best = Infinity;
    let p = 0;
    for (const h of homes) {
      const dx = (h.position[0] - t.position[0]) * mPerDegLon;
      const dy = (h.position[1] - t.position[1]) * M_PER_DEG_LAT;
      const d = dx * dx + dy * dy;
      if (d < best) { best = d; p = h.outage_p; }
    }
    return { ...t, outage_p: p };
  });
}
