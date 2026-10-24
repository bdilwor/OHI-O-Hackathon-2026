// Generic low-poly 3D models built in code (no model files). Units are metres, Z is up, origin at ground
// level under the object. Each mesh is a flat-shaded triangle list for deck.gl's SimpleMeshLayer.
// Per-vertex `colors` (0-1) are multiplied by the layer's color, so one mesh can carry shading
// (lighter walls, darker roof, door) while the layer color carries the outage severity.

function builder() {
  const pos = [];
  const nrm = [];
  const col = [];
  let shade = [1, 1, 1];
  const tri = (a, b, c) => {
    const u = [b[0] - a[0], b[1] - a[1], b[2] - a[2]];
    const v = [c[0] - a[0], c[1] - a[1], c[2] - a[2]];
    let n = [u[1] * v[2] - u[2] * v[1], u[2] * v[0] - u[0] * v[2], u[0] * v[1] - u[1] * v[0]];
    const len = Math.hypot(...n) || 1;
    n = n.map((x) => x / len);
    pos.push(...a, ...b, ...c);
    nrm.push(...n, ...n, ...n);
    col.push(...shade, ...shade, ...shade);
  };
  const quad = (a, b, c, d) => { tri(a, b, c); tri(a, c, d); };
  const tone = (s) => { shade = Array.isArray(s) ? s : [s, s, s]; };
  const mesh = () => ({
    attributes: {
      positions: { value: new Float32Array(pos), size: 3 },
      normals: { value: new Float32Array(nrm), size: 3 },
      colors: { value: new Float32Array(col), size: 3 },
    },
  });
  return { tri, quad, tone, mesh };
}

function addBox(b, [x0, x1], [y0, y1], [z0, z1]) {
  const p = (x, y, z) => [x, y, z];
  b.quad(p(x0, y0, z1), p(x1, y0, z1), p(x1, y1, z1), p(x0, y1, z1));
  b.quad(p(x0, y0, z0), p(x0, y1, z0), p(x1, y1, z0), p(x1, y0, z0));
  b.quad(p(x0, y0, z0), p(x1, y0, z0), p(x1, y0, z1), p(x0, y0, z1));
  b.quad(p(x1, y1, z0), p(x0, y1, z0), p(x0, y1, z1), p(x1, y1, z1));
  b.quad(p(x1, y0, z0), p(x1, y1, z0), p(x1, y1, z1), p(x1, y0, z1));
  b.quad(p(x0, y1, z0), p(x0, y0, z0), p(x0, y0, z1), p(x0, y1, z1));
}

function ring(r, z, seg, phase = 0, cx = 0, cy = 0) {
  return Array.from({ length: seg }, (_, i) => {
    const a = (i / seg) * Math.PI * 2 + phase;
    return [cx + r * Math.cos(a), cy + r * Math.sin(a), z];
  });
}

function addCylinder(b, r, z0, z1, seg = 8, cx = 0, cy = 0) {
  const lo = ring(r, z0, seg, 0, cx, cy);
  const hi = ring(r, z1, seg, 0, cx, cy);
  for (let i = 0; i < seg; i += 1) {
    const j = (i + 1) % seg;
    b.quad(lo[i], lo[j], hi[j], hi[i]);
    b.tri([cx, cy, z1], hi[i], hi[j]);
    b.tri([cx, cy, z0], lo[j], lo[i]);
  }
}

function addCone(b, r, z0, z1, seg = 8, phase = 0) {
  const base = ring(r, z0, seg, phase);
  for (let i = 0; i < seg; i += 1) {
    const j = (i + 1) % seg;
    b.tri(base[i], base[j], [0, 0, z1]);
    b.tri([0, 0, z0], base[j], base[i]);
  }
}

/** Low-poly "blob" crown: an octahedron subdivided once and pushed out to a squashed sphere. */
function addCrown(b, r, cz, squash = 0.85) {
  const top = [0, 0, 1]; const bot = [0, 0, -1];
  const eq = [[1, 0, 0], [0, 1, 0], [-1, 0, 0], [0, -1, 0]];
  const faces = [];
  for (let i = 0; i < 4; i += 1) {
    faces.push([eq[i], eq[(i + 1) % 4], top]);
    faces.push([eq[(i + 1) % 4], eq[i], bot]);
  }
  const norm = (v) => { const l = Math.hypot(...v); return v.map((x) => x / l); };
  const mid = (a, c) => norm([(a[0] + c[0]) / 2, (a[1] + c[1]) / 2, (a[2] + c[2]) / 2]);
  const place = (v) => [v[0] * r, v[1] * r, cz + v[2] * r * squash];
  for (const [a, c, d] of faces) {
    const ab = mid(a, c); const bc = mid(c, d); const ca = mid(d, a);
    for (const t of [[a, ab, ca], [ab, c, bc], [ca, bc, d], [ab, bc, ca]]) b.tri(...t.map(place));
  }
}

/** House with a gable roof, chimney and door. Walls 1.0, door/trim darker, roof shaded by the layer color. */
function addHouse(b, w, d, h, roofH) {
  const [x0, x1, y0, y1] = [-w / 2, w / 2, -d / 2, d / 2];
  b.tone(1);
  addBox(b, [x0, x1], [y0, y1], [0, h]);
  // gable roof along x, with eaves
  const e = 0.5;
  const [rx0, rx1, ry0, ry1] = [x0 - e, x1 + e, y0 - e, y1 + e];
  const ridge = h + roofH;
  b.tone(0.55);
  b.quad([rx0, ry0, h], [rx1, ry0, h], [rx1, 0, ridge], [rx0, 0, ridge]);
  b.quad([rx1, ry1, h], [rx0, ry1, h], [rx0, 0, ridge], [rx1, 0, ridge]);
  b.tone(0.9);
  b.tri([x0, y0, h], [x0, 0, ridge], [x0, y1, h]);
  b.tri([x1, y1, h], [x1, 0, ridge], [x1, y0, h]);
  // chimney + door
  b.tone(0.45);
  addBox(b, [x1 - 2.2, x1 - 1.2], [y1 * 0.15, y1 * 0.15 + 1], [h, ridge + 0.6]);
  b.tone(0.3);
  addBox(b, [-0.6, 0.6], [y0 - 0.05, y0 + 0.02], [0, 2.1]);
}

const make = (fn) => { const b = builder(); fn(b); return b.mesh(); };

export const MESHES = {
  house: make((b) => addHouse(b, 9, 7.5, 3.6, 2.8)),
  // window panes (separate layer: warm when powered, dark when a predicted outage)
  windows: make((b) => {
    for (const x of [-2.8, 2.8]) addBox(b, [x - 0.7, x + 0.7], [-3.83, -3.78], [1.4, 2.6]);
    for (const x of [-2.8, 2.8]) addBox(b, [x - 0.7, x + 0.7], [3.78, 3.83], [1.4, 2.6]);
  }),
  pineTrunk: make((b) => { b.tone([0.55, 0.42, 0.3]); addCylinder(b, 0.35, 0, 2.2, 6); }),
  pine: make((b) => {
    b.tone(0.82); addCone(b, 3.2, 1.8, 6.5, 8);
    b.tone(0.95); addCone(b, 2.5, 4.2, 8.8, 8, 0.4);
    b.tone(1.08); addCone(b, 1.7, 6.6, 11, 8, 0.8);
  }),
  leafyTrunk: make((b) => { b.tone([0.55, 0.42, 0.3]); addCylinder(b, 0.45, 0, 3.4, 6); }),
  leafy: make((b) => { b.tone(1); addCrown(b, 3.6, 6.2); b.tone(0.88); addCrown(b, 2.4, 8.4, 0.9); }),
  pole: make((b) => {
    b.tone(1); addCylinder(b, 0.2, 0, 10.5, 6);
    b.tone(0.7); addBox(b, [-1.5, 1.5], [-0.11, 0.11], [9.6, 9.85]);
  }),
  insulators: make((b) => {
    for (const x of [-1.2, 0, 1.2]) addCylinder(b, 0.13, 9.85, 10.35, 5, x, 0);
  }),
  transformer: make((b) => { b.tone(1); addCylinder(b, 0.7, 6.4, 8.0, 10); b.tone(0.6); addCylinder(b, 0.75, 7.95, 8.1, 10); }),
  device: make((b) => { b.tone(1); addBox(b, [-0.55, 0.55], [-0.55, 0.55], [7.6, 8.7]); }),
};
