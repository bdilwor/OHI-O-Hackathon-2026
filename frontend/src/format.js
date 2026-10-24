// Shared formatting: risk colors (map + cards) and a tiny safe markdown renderer for assistant replies.
import { createElement as h, Fragment } from 'react';

const STOPS = [
  [0.0, [46, 204, 143]],
  [0.01, [198, 216, 74]],
  [0.03, [245, 165, 36]],
  [0.08, [242, 113, 28]],
  [0.2, [240, 82, 82]],
];

/** Damage probability -> [r, g, b] on a green -> red scale (thresholds tuned to asset-level risk). */
export function riskColor(p) {
  if (p == null || Number.isNaN(p)) return [120, 135, 160];
  if (p >= STOPS[STOPS.length - 1][0]) return STOPS[STOPS.length - 1][1];
  for (let i = 1; i < STOPS.length; i += 1) {
    const [p1, c1] = STOPS[i];
    if (p <= p1) {
      const [p0, c0] = STOPS[i - 1];
      const t = (p - p0) / (p1 - p0);
      return c0.map((v, k) => Math.round(v + (c1[k] - v) * t));
    }
  }
  return STOPS[0][1];
}

// Homes are colored by chance of an outage, on the same bands the risk model reports (Low <5%, Medium <20%,
// High <50%, Very High 50%+). Equipment uses riskColor above (chance that one item is damaged).
const HOME_STOPS = [
  [0.0, [46, 204, 143]],
  [0.05, [198, 216, 74]],
  [0.2, [245, 165, 36]],
  [0.5, [240, 82, 82]],
];

export function outageColor(p) {
  if (p == null || Number.isNaN(p)) return [120, 135, 160];
  if (p >= 0.5) return HOME_STOPS[3][1];
  for (let i = 1; i < HOME_STOPS.length; i += 1) {
    const [p1, c1] = HOME_STOPS[i];
    if (p <= p1) {
      const [p0, c0] = HOME_STOPS[i - 1];
      const t = (p - p0) / (p1 - p0);
      return c0.map((v, k) => Math.round(v + (c1[k] - v) * t));
    }
  }
  return HOME_STOPS[0][1];
}

export const shade = (c, f) => c.map((v) => Math.round(v * f));

export const pct = (p, digits = 0) => (p == null ? '–' : `${(p * 100).toFixed(digits)}%`);

function inline(text, keyBase) {
  // **bold** only; everything else is plain text (React escapes it).
  return text.split(/(\*\*[^*]+\*\*)/g).map((part, i) =>
    part.startsWith('**') && part.endsWith('**')
      ? h('strong', { key: `${keyBase}-${i}` }, part.slice(2, -2))
      : h(Fragment, { key: `${keyBase}-${i}` }, part));
}

/** Minimal markdown: paragraphs, "- " bullets and **bold**. No HTML injection. */
export function renderReply(text) {
  const blocks = []; // [{type: 'p', text} | {type: 'ul', items: [text]}]
  text.split('\n').forEach((raw) => {
    const line = raw.trim();
    const bullet = line.match(/^[-*•]\s+(.*)$/);
    const last = blocks[blocks.length - 1];
    if (!line) blocks.push({ type: 'break' });
    else if (bullet && last?.type === 'ul') last.items.push(bullet[1]);
    else if (bullet) blocks.push({ type: 'ul', items: [bullet[1]] });
    else blocks.push({ type: 'p', text: line });
  });
  return blocks
    .filter((b) => b.type !== 'break')
    .map((b, i) => (b.type === 'ul'
      ? h('ul', { key: i }, b.items.map((item, j) => h('li', { key: j }, inline(item, `${i}-${j}`))))
      : h('p', { key: i }, inline(b.text, i))));
}
