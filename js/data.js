/**
 * data.js
 * Data loading and transformation utilities for the Hipsterhood visualisation.
 * ES module — imported by app.js.
 */

// ── Assign consistent colours to suburbs ──────────────────────────────────────
const COLOUR_PALETTE = [
  "#e15759", "#f28e2b", "#76b7b2", "#59a14f", "#edc948",
  "#b07aa1", "#ff9da7", "#9c755f", "#4e79a7", "#f1ce63",
  "#d37295", "#a0cbe8", "#86bcb6", "#8cd17d", "#499894",
  "#e6845e", "#d4a6c8", "#ffbe7d", "#c9d02c", "#ffa15a",
  "#bab0ac", "#19d3f3", "#ff6692", "#b6e880", "#ff97ff",
  "#fecb52", "#c73f0a", "#7da8a8", "#a9c574", "#72b7b2",
];

// ── Load combined.json and return enriched data structure ────────────────────
export async function loadData() {
  const [combined, geojson] = await Promise.all([
    d3.json("data/combined.json"),
    d3.json("data/melbourne-suburbs.geojson").catch(() => ({ type: "FeatureCollection", features: [] })),
  ]);

  const { years, suburbs, sources } = combined;

  // Enrich each suburb entry
  const enriched = suburbs.map((s, i) => {
    const scores = s.scores;   // array aligned to `years`
    const dataPoints = years.map((y, j) => ({ year: y, score: scores[j] ?? 0 }));
    const score2014 = scores[0] ?? 0;
    const score2026 = scores[scores.length - 1] ?? 0;
    const peakIdx   = scores.indexOf(Math.max(...scores));

    return {
      ...s,
      color:      s.color ?? COLOUR_PALETTE[i % COLOUR_PALETTE.length],
      dataPoints,
      score2014,
      score2026,
      peakYear:   years[peakIdx],
      totalScore: scores.reduce((a, b) => a + b, 0),
      trend:      computeTrend(score2014, score2026),
    };
  });

  return { years, suburbs: enriched, sources, geojson };
}

// ── Trend classification ─────────────────────────────────────────────────────
export function computeTrend(score2014, score2026) {
  if (score2014 < 1) return score2026 > 5 ? "rising" : "stable";
  if (score2026 > score2014 * 1.45) return "rising";
  if (score2026 < score2014 * 0.82) return "falling";
  return "stable";
}

// ── Trend display helpers ────────────────────────────────────────────────────
export const TREND_ARROWS = { rising: "↑", falling: "↓", stable: "→" };
export const TREND_COLOURS = { rising: "#59a14f", falling: "#e15759", stable: "#76b7b2" };
