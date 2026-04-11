/**
 * app.js — Hipsterhood Melbourne visualisation engine
 * D3.js v7 + Bootstrap 5. ES module.
 */

import { loadData, TREND_ARROWS, TREND_COLOURS } from "./data.js";

// ── Shared tooltip singleton ─────────────────────────────────────────────────
const tooltip = d3.select("body").append("div").attr("class", "d3-tooltip");

function showTooltip(html, event) {
  tooltip.classed("visible", true).html(html);
  moveTooltip(event);
}
function moveTooltip(event) {
  const tw = tooltip.node().offsetWidth;
  const th = tooltip.node().offsetHeight;
  const vw = window.innerWidth, vh = window.innerHeight;
  let x = event.clientX + 16, y = event.clientY - 10;
  if (x + tw + 8 > vw) x = event.clientX - tw - 16;
  if (y + th + 8 > vh) y = event.clientY - th - 10;
  tooltip.style("left", x + "px").style("top", y + "px");
}
function hideTooltip() { tooltip.classed("visible", false); }

// ── Responsive SVG dimensions ────────────────────────────────────────────────
function svgDims(containerId, targetHeight, margin) {
  const el = document.getElementById(containerId);
  const W  = el ? Math.max(el.clientWidth, 320) : 700;
  const H  = targetHeight;
  return {
    W, H,
    w: W - margin.left - margin.right,
    h: H - margin.top  - margin.bottom,
  };
}

// ══════════════════════════════════════════════════════════════════════════════
// VIEW 1 — Evolution Line Chart
// ══════════════════════════════════════════════════════════════════════════════

// Which suburbs are visible by default (excludes CBD)
const DEFAULT_VISIBLE = new Set([
  "Fitzroy", "Collingwood", "Brunswick", "Northcote",
  "Yarraville", "Cremorne", "Thornbury", "Richmond",
]);

function initEvolution(data) {
  const margin = { top: 24, right: 24, bottom: 44, left: 48 };
  const { W, H, w, h } = svgDims("evolution-chart", 480, margin);

  const svg = d3.select("#evolution-chart")
    .append("svg")
    .attr("viewBox", `0 0 ${W} ${H}`)
    .attr("preserveAspectRatio", "xMidYMid meet")
    .append("g")
    .attr("transform", `translate(${margin.left},${margin.top})`);

  // Scales
  const xScale = d3.scalePoint()
    .domain(data.years)
    .range([0, w])
    .padding(0.15);

  const allScores = data.suburbs.flatMap(s => s.dataPoints.map(d => d.score));
  const yScale = d3.scaleLinear()
    .domain([0, d3.max(allScores) * 1.08])
    .range([h, 0])
    .nice();

  // Grid
  svg.append("g").attr("class", "grid")
    .call(d3.axisLeft(yScale).tickSize(-w).tickFormat("").ticks(5));

  // COVID band
  const x2020 = xScale(2020), x2022 = xScale(2022);
  svg.append("rect")
    .attr("class", "covid-band")
    .attr("x", x2020).attr("y", 0)
    .attr("width", x2022 - x2020).attr("height", h);
  svg.append("text").attr("class", "covid-label")
    .attr("x", x2020 + 4).attr("y", 12)
    .text("COVID lockdowns");

  // Axes
  svg.append("g").attr("class", "axis")
    .attr("transform", `translate(0,${h})`)
    .call(d3.axisBottom(xScale).tickFormat(d3.format("d")));
  svg.append("g").attr("class", "axis")
    .call(d3.axisLeft(yScale).ticks(5));

  // Axis labels
  svg.append("text").attr("class", "axis-label")
    .attr("x", w / 2).attr("y", h + 38)
    .attr("text-anchor", "middle").text("Year");
  svg.append("text").attr("class", "axis-label")
    .attr("transform", "rotate(-90)")
    .attr("x", -h / 2).attr("y", -38)
    .attr("text-anchor", "middle").text("Pool share (pts/yr, Σ = 100)");

  // Line generator
  const lineGen = d3.line()
    .x(d => xScale(d.year))
    .y(d => yScale(d.score))
    .curve(d3.curveMonotoneX);

  // Draw lines (suburbs only — CBD handled separately)
  const suburbsToShow = data.suburbs.filter(s => s.type !== "cbd");
  const visibleSet = new Set(DEFAULT_VISIBLE);

  const paths = svg.selectAll(".suburb-line")
    .data(suburbsToShow)
    .join("path")
    .attr("class", s => `suburb-line ${visibleSet.has(s.name) ? "" : "dimmed"}`)
    .attr("d", s => lineGen(s.dataPoints))
    .attr("stroke", s => s.color)
    .on("pointerenter", (event, s) => {
      if (!visibleSet.has(s.name)) return;
      paths.classed("dimmed", d => d.name !== s.name && !visibleSet.has(d.name));
      paths.filter(d => d.name === s.name).classed("highlighted", true);
    })
    .on("pointerleave", () => {
      paths.classed("highlighted", false);
      paths.classed("dimmed", s => !visibleSet.has(s.name));
    });

  // Invisible wider hit-area for hover
  svg.selectAll(".suburb-hit")
    .data(suburbsToShow)
    .join("path")
    .attr("fill", "none")
    .attr("stroke", "transparent")
    .attr("stroke-width", 12)
    .attr("d", s => lineGen(s.dataPoints))
    .on("pointerenter", (event, s) => {
      if (!visibleSet.has(s.name)) return;
      const lastPt = s.dataPoints[s.dataPoints.length - 1];
      showTooltip(
        `<strong style="color:${s.color}">${s.name}</strong><br>` +
        `2014: ${s.score2014.toFixed(1)} → 2026: ${s.score2026.toFixed(1)}<br>` +
        `Trend: ${TREND_ARROWS[s.trend]} <span style="color:${TREND_COLOURS[s.trend]}">${s.trend}</span>`,
        event
      );
    })
    .on("pointermove", moveTooltip)
    .on("pointerleave", hideTooltip);

  // Year hover crosshair + label overlay
  const vLine = svg.append("line").attr("class", "hover-crosshair")
    .attr("y1", 0).attr("y2", h);

  // Overlay for tracking mouse
  svg.append("rect")
    .attr("width", w).attr("height", h)
    .attr("fill", "none").attr("pointer-events", "all")
    .on("pointermove", function (event) {
      const [mx] = d3.pointer(event, this);
      const nearestYear = data.years.reduce((a, b) =>
        Math.abs(xScale(a) - mx) < Math.abs(xScale(b) - mx) ? a : b
      );
      vLine.attr("x1", xScale(nearestYear)).attr("x2", xScale(nearestYear)).attr("opacity", 0.7);

      const visible = suburbsToShow.filter(s => visibleSet.has(s.name));
      const rows = [...visible]
        .map(s => ({ name: s.name, score: s.dataPoints.find(d => d.year === nearestYear)?.score ?? 0, color: s.color }))
        .sort((a, b) => b.score - a.score)
        .slice(0, 8)
        .map(s => `<span style="color:${s.color}">■</span> ${s.name}: <b>${s.score.toFixed(1)}</b>`)
        .join("<br>");

      showTooltip(`<strong>${nearestYear}</strong><br>${rows}`, event);
    })
    .on("pointerleave", () => {
      vLine.attr("opacity", 0);
      hideTooltip();
    });

  // ── Legend ──
  buildEvolutionLegend(data, suburbsToShow, visibleSet, paths);
}

function buildEvolutionLegend(data, suburbs, visibleSet, pathsSel) {
  const legend = d3.select("#evolution-legend");
  legend.selectAll("*").remove();

  // "Show all / Hide all" buttons
  const btnRow = legend.append("div").style("margin-bottom", "8px");
  btnRow.append("button").attr("class", "btn btn-sm btn-outline-secondary me-1").text("All on")
    .on("click", () => {
      suburbs.forEach(s => visibleSet.add(s.name));
      refresh();
    });
  btnRow.append("button").attr("class", "btn btn-sm btn-outline-secondary").text("Reset")
    .on("click", () => {
      visibleSet.clear();
      DEFAULT_VISIBLE.forEach(n => visibleSet.add(n));
      refresh();
    });

  function refresh() {
    legend.selectAll(".legend-item").classed("dimmed", s => !visibleSet.has(s.name));
    pathsSel.classed("dimmed", s => !visibleSet.has(s.name));
  }

  // Sort legend by 2026 score descending
  const sorted = [...suburbs].sort((a, b) => b.score2026 - a.score2026);

  legend.selectAll(".legend-item")
    .data(sorted)
    .join("div")
    .attr("class", s => `legend-item ${visibleSet.has(s.name) ? "" : "dimmed"}`)
    .html(s =>
      `<span class="legend-swatch" style="background:${s.color}"></span>` +
      `<span class="legend-name">${s.name}</span>` +
      `<span class="legend-score">${s.score2026.toFixed(0)}</span>` +
      `<span class="legend-trend-arrow" style="color:${TREND_COLOURS[s.trend]}">${TREND_ARROWS[s.trend]}</span>`
    )
    .on("click", (event, s) => {
      if (visibleSet.has(s.name)) visibleSet.delete(s.name);
      else visibleSet.add(s.name);
      refresh();
    });
}

// ══════════════════════════════════════════════════════════════════════════════
// VIEW 2 — Bump / Rank Chart (ladder of suburb positions over time)
// ══════════════════════════════════════════════════════════════════════════════

function initRankings(data) {
  const N = 15;  // top N non-CBD suburbs to show
  const { years } = data;

  // Top N suburbs by cumulative score (excl. CBD)
  const suburbs = data.suburbs
    .filter(s => s.type !== "cbd")
    .sort((a, b) => b.totalScore - a.totalScore)
    .slice(0, N);

  // Compute rank per suburb per year
  const ranksByYear = {};
  years.forEach(year => {
    const idx = years.indexOf(year);
    const sorted = [...suburbs].sort(
      (a, b) => b.dataPoints[idx].score - a.dataPoints[idx].score
    );
    ranksByYear[year] = Object.fromEntries(sorted.map((s, i) => [s.name, i + 1]));
  });

  const enriched = suburbs.map(s => ({
    ...s,
    ranks: years.map(y => ranksByYear[y][s.name]),
  }));

  const margin = { top: 20, right: 130, bottom: 44, left: 48 };
  const { W, H, w, h } = svgDims("rankings-chart", 540, margin);

  const svg = d3.select("#rankings-chart")
    .append("svg")
    .attr("viewBox", `0 0 ${W} ${H}`)
    .attr("preserveAspectRatio", "xMidYMid meet")
    .append("g")
    .attr("transform", `translate(${margin.left},${margin.top})`);

  const xScale = d3.scalePoint().domain(years).range([0, w]).padding(0.2);
  // Rank 1 at top → map rank N to y=h
  const yScale = d3.scalePoint()
    .domain(d3.range(1, N + 1))
    .range([0, h])
    .padding(0.25);

  // Grid lines (horizontal, one per rank)
  svg.append("g").attr("class", "grid")
    .selectAll("line")
    .data(d3.range(1, N + 1))
    .join("line")
    .attr("x1", 0).attr("x2", w)
    .attr("y1", d => yScale(d)).attr("y2", d => yScale(d))
    .attr("stroke", "#e8e8e8").attr("stroke-width", 1);

  // Axes
  svg.append("g").attr("class", "axis")
    .attr("transform", `translate(0,${h})`)
    .call(d3.axisBottom(xScale).tickFormat(d3.format("d")));
  svg.append("g").attr("class", "axis")
    .call(d3.axisLeft(yScale).tickFormat(d => `#${d}`));

  svg.append("text").attr("class", "axis-label")
    .attr("x", w / 2).attr("y", h + 38)
    .attr("text-anchor", "middle").text("Year");
  svg.append("text").attr("class", "axis-label")
    .attr("transform", "rotate(-90)")
    .attr("x", -h / 2).attr("y", -36)
    .attr("text-anchor", "middle").text("Rank");

  // Track which suburb is highlighted (click to toggle)
  let highlighted = null;

  const lineGen = d3.line()
    .x((_, i) => xScale(years[i]))
    .y(d => yScale(d))
    .curve(d3.curveMonotoneX);

  // Lines
  const lines = svg.selectAll(".bump-line")
    .data(enriched, s => s.name)
    .join("path")
    .attr("class", "bump-line")
    .attr("stroke", s => s.color)
    .attr("d", s => lineGen(s.ranks))
    .on("click", (_, s) => toggleHighlight(s.name))
    .on("pointerenter", function (event, s) {
      if (!highlighted) d3.select(this).attr("stroke-width", 4);
    })
    .on("pointerleave", function () {
      if (!highlighted) d3.select(this).attr("stroke-width", 2.5);
    });

  // Dots + rank badges
  enriched.forEach(s => {
    years.forEach((year, i) => {
      const cx = xScale(year), cy = yScale(s.ranks[i]);
      const score = s.dataPoints[i].score;

      svg.append("circle")
        .attr("class", "bump-dot")
        .attr("cx", cx).attr("cy", cy).attr("r", 8)
        .attr("fill", s.color).attr("stroke", "#fff").attr("stroke-width", 1.5)
        .attr("data-suburb", s.name)
        .on("click", () => toggleHighlight(s.name))
        .on("pointerenter", event => showTooltip(
          `<strong>${s.name}</strong><br>` +
          `${year}: Rank <b>#${s.ranks[i]}</b><br>` +
          `Pool share: <b>${score.toFixed(2)} pts</b>`,
          event
        ))
        .on("pointermove", moveTooltip)
        .on("pointerleave", hideTooltip);

      svg.append("text")
        .attr("class", "bump-rank-badge")
        .attr("x", cx).attr("y", cy)
        .attr("data-suburb", s.name)
        .text(s.ranks[i]);
    });
  });

  // Labels at the 2026 end
  const lastIdx = years.length - 1;
  svg.selectAll(".bump-label")
    .data(enriched, s => s.name)
    .join("text")
    .attr("class", "bump-label")
    .attr("x", xScale(years[lastIdx]) + 14)
    .attr("y", s => yScale(s.ranks[lastIdx]))
    .attr("fill", s => s.color)
    .attr("data-suburb", s.name)
    .text(s => s.name);

  function toggleHighlight(name) {
    highlighted = highlighted === name ? null : name;
    lines.attr("class", s => {
      if (!highlighted) return "bump-line";
      return `bump-line ${s.name === highlighted ? "highlighted" : "dimmed"}`;
    }).attr("stroke-width", 2.5);

    svg.selectAll(".bump-dot").attr("opacity", function () {
      if (!highlighted) return 1;
      return this.getAttribute("data-suburb") === highlighted ? 1 : 0.08;
    });
    svg.selectAll(".bump-rank-badge").attr("opacity", function () {
      if (!highlighted) return 1;
      return this.getAttribute("data-suburb") === highlighted ? 1 : 0.08;
    });
    svg.selectAll(".bump-label").attr("opacity", function () {
      if (!highlighted) return 1;
      return this.getAttribute("data-suburb") === highlighted ? 1 : 0.12;
    });
  }
}

// ══════════════════════════════════════════════════════════════════════════════
// VIEW 3 — Choropleth Map
// ══════════════════════════════════════════════════════════════════════════════

function initMap(data) {
  const { geojson, years, suburbs } = data;
  if (!geojson.features.length) {
    d3.select("#map-chart").html(
      '<div class="alert alert-secondary m-3">Map data not available. ' +
      "Run <code>scripts/generate_snapshots.py</code> with network access to generate suburb polygons.</div>"
    );
    return;
  }

  let currentYearIdx = years.length - 1;
  let playing = false;
  let playTimer = null;

  const margin = { top: 10, right: 20, bottom: 20, left: 20 };
  const { W, H, w, h } = svgDims("map-chart", 520, margin);

  const svg = d3.select("#map-chart")
    .append("svg")
    .attr("viewBox", `0 0 ${W} ${H}`)
    .attr("preserveAspectRatio", "xMidYMid meet")
    .append("g")
    .attr("transform", `translate(${margin.left},${margin.top})`);

  // Fit projection to our GeoJSON features
  const projection = d3.geoMercator().fitSize([w, h - 30], geojson);
  const pathGen = d3.geoPath().projection(projection);

  // Colour scale: light yellow → burnt orange → dark red (hipster heat)
  const colourScale = d3.scaleSequential()
    .domain([0, 100])
    .interpolator(d3.interpolateYlOrRd);

  // Build lookup for score by suburb name
  function getScore(suburbName, yearIdx) {
    const s = suburbs.find(d => d.name === suburbName);
    return s ? (s.dataPoints[yearIdx]?.score ?? 0) : 0;
  }

  // Draw polygons
  const polygons = svg.selectAll(".suburb-polygon")
    .data(geojson.features)
    .join("path")
    .attr("class", "suburb-polygon")
    .attr("d", pathGen)
    .on("pointerenter", function (event, f) {
      const name  = f.properties.name;
      const score = getScore(name, currentYearIdx);
      const year  = years[currentYearIdx];
      const s = suburbs.find(d => d.name === name);
      const delta2014 = s ? (score - (s.dataPoints[0]?.score ?? 0)).toFixed(1) : "?";
      const sign = delta2014 > 0 ? "+" : "";
      showTooltip(
        `<strong>${name}</strong><br>` +
        `${year} score: <b>${score.toFixed(1)}</b><br>` +
        `vs 2014: <b>${sign}${delta2014}</b>`,
        event
      );
    })
    .on("pointermove", moveTooltip)
    .on("pointerleave", hideTooltip);

  // Suburb name labels (only for reasonably large polygons)
  const labels = svg.selectAll(".map-suburb-label")
    .data(geojson.features)
    .join("text")
    .attr("class", "map-suburb-label")
    .attr("font-size", 8)
    .attr("fill", "#444")
    .attr("text-anchor", "middle")
    .attr("pointer-events", "none")
    .attr("transform", f => {
      const c = pathGen.centroid(f);
      return isNaN(c[0]) ? "translate(-9999,-9999)" : `translate(${c[0]},${c[1]})`;
    })
    .text(f => f.properties.name);

  // Year display
  const mapYearText = d3.select("#map-year-display");

  // Gradient legend
  buildMapLegend(svg, w, h, colourScale);

  function updateMap(yearIdx) {
    currentYearIdx = yearIdx;
    const year = years[yearIdx];
    mapYearText.text(year);
    d3.select("#map-year-slider").property("value", yearIdx);

    polygons
      .transition().duration(350)
      .attr("fill", f => {
        const score = getScore(f.properties.name, yearIdx);
        return score > 0 ? colourScale(score) : "#e0e0e0";
      });
  }

  // Slider
  d3.select("#map-year-slider")
    .attr("min", 0).attr("max", years.length - 1).attr("value", currentYearIdx)
    .on("input", function () { stopPlay(); updateMap(+this.value); });

  // Play/Pause
  d3.select("#map-play-btn").on("click", () => {
    if (playing) stopPlay(); else startPlay();
  });

  function startPlay() {
    playing = true;
    d3.select("#map-play-btn").text("⏸ Pause");
    if (currentYearIdx >= years.length - 1) currentYearIdx = -1;
    playTimer = setInterval(() => {
      currentYearIdx++;
      updateMap(currentYearIdx);
      if (currentYearIdx >= years.length - 1) stopPlay();
    }, 1200);
  }
  function stopPlay() {
    playing = false;
    clearInterval(playTimer);
    d3.select("#map-play-btn").text("▶ Play");
  }

  updateMap(currentYearIdx);
}

function buildMapLegend(svg, w, h, colourScale) {
  const legendW = 160, legendH = 10;
  const lx = w - legendW - 10, ly = h - 22;

  const defs = svg.append("defs");
  const grad = defs.append("linearGradient").attr("id", "map-legend-grad");
  const stops = d3.range(0, 1.01, 0.1);
  stops.forEach(t => {
    grad.append("stop")
      .attr("offset", `${t * 100}%`)
      .attr("stop-color", colourScale(t * 100));
  });

  svg.append("rect").attr("x", lx).attr("y", ly)
    .attr("width", legendW).attr("height", legendH)
    .attr("fill", "url(#map-legend-grad)").attr("rx", 2);

  svg.append("text").attr("x", lx).attr("y", ly - 3).attr("font-size", 9).attr("fill", "#666").text("Low");
  svg.append("text").attr("x", lx + legendW).attr("y", ly - 3).attr("font-size", 9).attr("fill", "#666").attr("text-anchor", "end").text("High");
  svg.append("text").attr("x", lx + legendW / 2).attr("y", ly + legendH + 12).attr("font-size", 9).attr("fill", "#888").attr("text-anchor", "middle").text("Hipster score");
}

// ══════════════════════════════════════════════════════════════════════════════
// VIEW 4 — Head-to-Head Comparison
// ══════════════════════════════════════════════════════════════════════════════

function initHeadToHead(data) {
  const suburbsFiltered = data.suburbs.filter(s => s.type !== "cbd");

  // Populate selects
  ["h2h-suburb-a", "h2h-suburb-b"].forEach((id, i) => {
    const sel = document.getElementById(id);
    suburbsFiltered.forEach(s => {
      const opt = document.createElement("option");
      opt.value = opt.textContent = s.name;
      sel.appendChild(opt);
    });
    // Default: A=Fitzroy, B=Brunswick
    const defaults = ["Fitzroy", "Brunswick"];
    sel.value = defaults[i] ?? suburbsFiltered[i]?.name;
  });

  function redraw() {
    d3.select("#h2h-chart").selectAll("*").remove();
    d3.select("#h2h-stats").selectAll("*").remove();
    const nameA = document.getElementById("h2h-suburb-a").value;
    const nameB = document.getElementById("h2h-suburb-b").value;
    if (!nameA || !nameB) return;
    const subA = suburbsFiltered.find(s => s.name === nameA);
    const subB = suburbsFiltered.find(s => s.name === nameB);
    if (!subA || !subB) return;
    drawH2H(subA, subB, data.years);
    drawH2HStats(subA, subB, data.years);
  }

  document.getElementById("h2h-suburb-a").addEventListener("change", redraw);
  document.getElementById("h2h-suburb-b").addEventListener("change", redraw);
  redraw();
}

function drawH2H(subA, subB, years) {
  const margin = { top: 24, right: 30, bottom: 44, left: 48 };
  const { W, H, w, h } = svgDims("h2h-chart", 360, margin);

  const svg = d3.select("#h2h-chart")
    .append("svg")
    .attr("viewBox", `0 0 ${W} ${H}`)
    .attr("preserveAspectRatio", "xMidYMid meet")
    .append("g")
    .attr("transform", `translate(${margin.left},${margin.top})`);

  const xScale = d3.scalePoint().domain(years).range([0, w]).padding(0.15);
  const yMax = d3.max([...subA.dataPoints, ...subB.dataPoints], d => d.score) * 1.12;
  const yScale = d3.scaleLinear().domain([0, yMax]).range([h, 0]).nice();

  // Grid
  svg.append("g").attr("class", "grid")
    .call(d3.axisLeft(yScale).tickSize(-w).tickFormat("").ticks(5));

  // Dominance fill areas
  // We interpolate intersection points for a continuous fill
  const combined = years.map((y, i) => ({
    year: y,
    sA: subA.dataPoints[i].score,
    sB: subB.dataPoints[i].score,
  }));

  const areaA = d3.area()
    .x(d => xScale(d.year)).curve(d3.curveMonotoneX)
    .y0(d => yScale(Math.min(d.sA, d.sB)))
    .y1(d => yScale(d.sA))
    .defined(d => d.sA >= d.sB);

  const areaB = d3.area()
    .x(d => xScale(d.year)).curve(d3.curveMonotoneX)
    .y0(d => yScale(Math.min(d.sA, d.sB)))
    .y1(d => yScale(d.sB))
    .defined(d => d.sB > d.sA);

  svg.append("path").datum(combined).attr("class", "dominance-area-a")
    .attr("fill", subA.color).attr("opacity", 0.22).attr("d", areaA);
  svg.append("path").datum(combined).attr("class", "dominance-area-b")
    .attr("fill", subB.color).attr("opacity", 0.22).attr("d", areaB);

  // Lines
  const lineGen = d3.line()
    .x(d => xScale(d.year)).y(d => yScale(d.score)).curve(d3.curveMonotoneX);

  [subA, subB].forEach(sub => {
    svg.append("path").datum(sub.dataPoints)
      .attr("class", "suburb-line")
      .attr("stroke", sub.color)
      .attr("d", lineGen);

    // Dots
    svg.selectAll(`.dot-${sub.name.replace(/\s/g, "")}`)
      .data(sub.dataPoints)
      .join("circle")
      .attr("cx", d => xScale(d.year))
      .attr("cy", d => yScale(d.score))
      .attr("r", 4)
      .attr("fill", sub.color)
      .attr("stroke", "#fff").attr("stroke-width", 1.5)
      .on("pointerenter", (event, d) => showTooltip(
        `<strong style="color:${sub.color}">${sub.name}</strong><br>${d.year}: <b>${d.score.toFixed(1)}</b>`,
        event
      ))
      .on("pointermove", moveTooltip)
      .on("pointerleave", hideTooltip);
  });

  // End-of-line labels
  [subA, subB].forEach(sub => {
    const last = sub.dataPoints[sub.dataPoints.length - 1];
    svg.append("text")
      .attr("class", "line-end-label")
      .attr("x", xScale(last.year) + 7)
      .attr("y", yScale(last.score) + 4)
      .attr("font-size", 11)
      .attr("fill", sub.color)
      .text(sub.name);
  });

  // Axes
  svg.append("g").attr("class", "axis")
    .attr("transform", `translate(0,${h})`)
    .call(d3.axisBottom(xScale).tickFormat(d3.format("d")));
  svg.append("g").attr("class", "axis")
    .call(d3.axisLeft(yScale).ticks(5));

  svg.append("text").attr("class", "axis-label")
    .attr("x", w / 2).attr("y", h + 38)
    .attr("text-anchor", "middle").text("Year");
  svg.append("text").attr("class", "axis-label")
    .attr("transform", "rotate(-90)")
    .attr("x", -h / 2).attr("y", -38)
    .attr("text-anchor", "middle").text("Hipster Score");
}

function drawH2HStats(subA, subB, years) {
  const statsEl = d3.select("#h2h-stats");

  const aLeading = years.filter((y, i) => subA.dataPoints[i].score > subB.dataPoints[i].score).length;
  const bLeading = years.length - aLeading;

  const deltaA = subA.score2026 - subA.score2014;
  const deltaB = subB.score2026 - subB.score2014;

  function card(label, valA, valB) {
    const el = statsEl.append("div").attr("class", "h2h-stat-card col-6 col-md-3");
    el.append("div").attr("class", "stat-label").text(label);
    const row = el.append("div").attr("class", "d-flex gap-3 mt-1");
    row.append("span").attr("class", "stat-value").style("color", subA.color)
      .text(typeof valA === "number" ? valA.toFixed(1) : valA);
    row.append("span").attr("class", "stat-value").style("color", subB.color)
      .text(typeof valB === "number" ? valB.toFixed(1) : valB);
  }

  statsEl.append("div").attr("class", "row g-2");

  card("2026 Score", subA.score2026, subB.score2026);
  card("Change vs 2014", deltaA, deltaB);
  card("Peak Year", subA.peakYear, subB.peakYear);
  card("Years Leading", aLeading, bLeading);
}

// ══════════════════════════════════════════════════════════════════════════════
// ── Entry point ───────────────────────────────────────────────────────────────
document.addEventListener("DOMContentLoaded", async () => {
  // Show loading spinner in each chart container
  ["evolution-chart", "rankings-chart", "map-chart", "h2h-chart"].forEach(id => {
    const el = document.getElementById(id);
    if (el) el.innerHTML = '<div class="loading-spinner"><div class="spinner-border text-secondary"></div></div>';
  });

  let data;
  try {
    data = await loadData();
  } catch (err) {
    console.error("Failed to load data:", err);
    document.getElementById("evolution-chart").innerHTML =
      `<div class="alert alert-danger m-3">Failed to load data. Make sure you're running from a local HTTP server.<br><code>python3 -m http.server 8000</code></div>`;
    return;
  }

  // Clear spinners
  ["evolution-chart", "rankings-chart", "map-chart", "h2h-chart"].forEach(id => {
    const el = document.getElementById(id);
    if (el) el.innerHTML = "";
  });

  // Render the first (active) tab immediately
  initEvolution(data);

  // Lazy render remaining tabs when first shown.
  // Attach directly to each button by ID — reliable regardless of Bootstrap
  // event bubbling behaviour (each button only fires its own shown.bs.tab).
  let rankingsInit = false, mapInit = false, h2hInit = false;

  document.getElementById("tab-rank-btn")?.addEventListener("shown.bs.tab", () => {
    if (!rankingsInit) { rankingsInit = true; initRankings(data); }
  });
  document.getElementById("tab-map-btn")?.addEventListener("shown.bs.tab", () => {
    if (!mapInit) { mapInit = true; initMap(data); }
  });
  document.getElementById("tab-h2h-btn")?.addEventListener("shown.bs.tab", () => {
    if (!h2hInit) { h2hInit = true; initHeadToHead(data); }
  });
});
