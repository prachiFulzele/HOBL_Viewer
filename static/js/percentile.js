// ── Percentile Distribution view ────────────────────────────────────────────
//
// X axis = percentile (0–100), Y axis = the metric's value. For a single
// scenario, every Device + RAM config is overlaid as its own colored line: the
// metric's values across the selected iterations ("Last N Iterations") are
// sorted ascending and spread across the percentile axis (rank i of n maps to
// percentile i/(n-1)*100). P50/P70/P90 are interpolated and annotated.
//
// Metrics are chosen via three type dropdowns — Perf, Power Calculation, Power
// Metrics (rails). A radio circle to the left of each dropdown's title selects
// which one is active; only one type is active at a time. Each dropdown is a
// checkbox multi-select (like the Device / RAM / Scenario filters): the active
// type defaults to ALL of its metrics checked, and the user can check any subset
// to stack exactly that many charts.
//
// Depends on shared helpers from common.js: activeFilterSummary, escapeHtml,
// valueAtPercentile, fmtNum, resultsDiv, pctMetricType, pctMetricSel.

const PCT_TYPE_LABEL = { perf: 'Perf Metrics', calc: 'Power Calculation', rail: 'Power Metrics' };
const PCT_TYPE_ORDER = ['perf', 'calc', 'rail'];

// Distinct colors per Device + RAM config combo (so 8 GB and 16 GB of the same
// device are clearly different colors, each with its own line).
const COMBO_COLORS = ['#1f77b4', '#ff7f0e', '#2ca02c', '#d62728', '#9467bd',
    '#8c564b', '#e377c2', '#17becf', '#bcbd22', '#393b79', '#637939', '#8c6d31'];
let pctComboColorMap = {};
function buildComboColorMap(rows) {
    const combos = Array.from(new Set(rows.map(r => r.Device + '\u0001' + r.Ram))).sort();
    pctComboColorMap = {};
    combos.forEach((c, i) => { pctComboColorMap[c] = COMBO_COLORS[i % COMBO_COLORS.length]; });
}
function colorForCombo(device, ram) {
    return pctComboColorMap[device + '\u0001' + ram] || COMBO_COLORS[0];
}

// Group rows by metric name, keeping a name list (first-seen, optionally sorted).
function pctGroupByName(rows, sortNames) {
    const byName = {}, names = [];
    rows.forEach(r => {
        if (!byName[r.MetricName]) { byName[r.MetricName] = []; names.push(r.MetricName); }
        byName[r.MetricName].push(r);
    });
    if (sortNames) names.sort((a, b) => a.localeCompare(b));
    return { names, byName };
}

function renderPercentile(metrics) {
    // The percentile view describes ONE scenario at a time (overlaying every
    // device / RAM config). Bail out if the fetched data spans multiple.
    const scenarios = Array.from(new Set(metrics.map(m => m.Scenario)));
    if (scenarios.length > 1) {
        resultsDiv.innerHTML =
            `<div class="results-summary">${activeFilterSummary()} &nbsp;|&nbsp; ${metrics.length} metric(s)</div>` +
            `<div class="chart-note">The Percentile Distribution view shows one scenario at a time. ` +
            `Please select a single <strong>Scenario</strong> and click <strong>Apply</strong> ` +
            `(currently ${scenarios.length} scenarios match).</div>`;
        return;
    }
    const scen = scenarios[0];

    // Build the three metric-type groups for this scenario. Perf uses the curated
    // first / P50-P70-P90 rows (same as the Table view); calc + rails pass through.
    const perfRows = (typeof curatedPerfMetricRows === 'function' && currentTable)
        ? curatedPerfMetricRows(currentTable).filter(r => r.Scenario === scen)
        : [];
    const calcRows = metrics.filter(m => m.MetricType === 'PowerCalculation' && m.Scenario === scen);
    const railRows = metrics.filter(m => m.MetricType === 'PowerMetrics' && m.Scenario === scen);

    const groups = {
        perf: pctGroupByName(perfRows, false),
        calc: pctGroupByName(calcRows, true),
        rail: pctGroupByName(railRows, true),
    };

    // Pick the active metric-type dropdown. Keep the persisted one if it still
    // has metrics for this scenario, otherwise fall back to the first non-empty
    // type (so a chart is shown immediately). Each dropdown is a checkbox
    // multi-select; activating one defaults to ALL of its metrics checked.
    if (!pctMetricType || !groups[pctMetricType] || !groups[pctMetricType].names.length) {
        pctMetricType = PCT_TYPE_ORDER.find(t => groups[t].names.length) || null;
        if (pctMetricType) pctMetricSel[pctMetricType] = new Set(groups[pctMetricType].names);
    }
    // Ensure the active type has a valid selection set (pruned to current names,
    // defaulting to all when empty or unset — e.g. after a scenario change).
    if (pctMetricType) {
        const names = groups[pctMetricType].names;
        let sel = pctMetricSel[pctMetricType];
        sel = sel ? new Set([...sel].filter(n => names.includes(n))) : new Set();
        if (!sel.size) sel = new Set(names);
        pctMetricSel[pctMetricType] = sel;
    }

    // Build the three dropdowns: a radio circle + title, then a checkbox
    // multi-select. Only the active type's multi-select is enabled.
    const ddHtml = PCT_TYPE_ORDER.map(t => {
        const g = groups[t];
        const isActive = pctMetricType === t;
        const hasMetrics = g.names.length > 0;
        const sel = isActive ? pctMetricSel[t] : new Set();
        const items = g.names.map(n =>
            `<label class="ms-item"><input type="checkbox" value="${escapeHtml(n)}"${sel.has(n) ? ' checked' : ''}>` +
            `<span>${escapeHtml(n)}</span></label>`).join('');
        return `
            <div class="pct-dd${isActive ? ' is-active' : ''}${hasMetrics ? '' : ' is-empty'}" data-type="${t}">
                <label class="pct-dd-head">
                    <input type="radio" name="pctType" class="pct-radio" data-type="${t}"${isActive ? ' checked' : ''}${hasMetrics ? '' : ' disabled'}>
                    <span class="pct-dd-title">${PCT_TYPE_LABEL[t]}${hasMetrics ? '' : ' <span class="pct-dd-empty">(none)</span>'}</span>
                </label>
                <div class="pct-msel" data-type="${t}">
                    <button type="button" class="ms-toggle pct-mtoggle"${isActive && hasMetrics ? '' : ' disabled'}>
                        <span class="ms-text">${pctToggleText(t, g, sel, isActive)}</span>
                        <span class="ms-caret">\u25be</span>
                    </button>
                    <div class="ms-panel pct-mpanel" style="display:none">
                        <div class="ms-actions"><a href="#" class="pct-selall">Select all</a><a href="#" class="pct-clr">Clear</a></div>
                        <div class="ms-list">${items}</div>
                    </div>
                </div>
            </div>`;
    }).join('');

    resultsDiv.innerHTML = `
        <div class="results-summary">${activeFilterSummary()} &nbsp;|&nbsp; ${metrics.length} metric(s)</div>
        <div class="chart-card">
            <div class="pct-controls pct-controls-multi">${ddHtml}</div>
            <div class="pct-sub">Values across the selected iterations, sorted and spread over the 0\u2013100 percentile axis. Each Device / RAM config is a separate colored line. Click a dropdown's circle to make it active (it starts with <strong>all</strong> its metrics shown); then check or uncheck metrics to stack exactly the charts you want. Set <strong>Last N Iterations</strong> to choose how many recent iterations to include.</div>
            <div id="pctChartsArea"></div>
        </div>`;

    wirePctControls(groups);
    renderPctCharts(groups);
}

// Summary text for a dropdown's toggle button.
function pctToggleText(type, group, sel, isActive) {
    const total = group.names.length;
    if (!total) return '(none)';
    if (!isActive) return `All ${PCT_TYPE_LABEL[type]} (${total})`;
    if (sel.size === 0) return 'None selected';
    if (sel.size === total) return `All ${PCT_TYPE_LABEL[type]} (${total})`;
    if (sel.size === 1) return Array.from(sel)[0];
    return `${sel.size} selected`;
}

// Wire up the radio circles, the active multi-select panel, and outside-click
// closing. Radio changes trigger a full re-render; checkbox changes update state
// and redraw only the charts (so the panel stays open while picking metrics).
function wirePctControls(groups) {
    // Radio circle: activate this type, default to all metrics, full re-render.
    resultsDiv.querySelectorAll('.pct-radio').forEach(radio => {
        radio.addEventListener('change', () => {
            if (!radio.checked) return;
            const t = radio.dataset.type;
            pctMetricType = t;
            pctMetricSel[t] = new Set(groups[t].names);
            renderPercentile(currentMetrics);
        });
    });

    const activeDd = resultsDiv.querySelector('.pct-dd.is-active');
    if (!activeDd) { wirePctOutsideClose(); return; }
    const toggle = activeDd.querySelector('.pct-mtoggle');
    const panel = activeDd.querySelector('.pct-mpanel');
    const textEl = activeDd.querySelector('.ms-text');
    const group = groups[pctMetricType];
    const sel = pctMetricSel[pctMetricType];

    if (toggle && panel) {
        toggle.addEventListener('click', e => {
            e.stopPropagation();
            const open = panel.style.display !== 'none';
            document.querySelectorAll('.pct-mpanel').forEach(p => p.style.display = 'none');
            panel.style.display = open ? 'none' : 'block';
        });
        panel.addEventListener('click', e => e.stopPropagation());
    }

    const refresh = () => {
        if (textEl) textEl.textContent = pctToggleText(pctMetricType, group, sel, true);
        renderPctCharts(groups);
    };

    panel.querySelectorAll('input[type=checkbox]').forEach(cb => {
        cb.addEventListener('change', () => {
            if (cb.checked) sel.add(cb.value); else sel.delete(cb.value);
            refresh();
        });
    });
    const selAll = panel.querySelector('.pct-selall');
    const clr = panel.querySelector('.pct-clr');
    if (selAll) selAll.addEventListener('click', e => {
        e.preventDefault();
        group.names.forEach(n => sel.add(n));
        panel.querySelectorAll('input[type=checkbox]').forEach(cb => cb.checked = true);
        refresh();
    });
    if (clr) clr.addEventListener('click', e => {
        e.preventDefault();
        sel.clear();
        panel.querySelectorAll('input[type=checkbox]').forEach(cb => cb.checked = false);
        refresh();
    });

    wirePctOutsideClose();
}

// Close any open percentile multi-select panel when clicking elsewhere. Wired
// once on the document (guarded), since percentile re-renders frequently.
function wirePctOutsideClose() {
    if (window._pctOutsideWired) return;
    window._pctOutsideWired = true;
    document.addEventListener('click', () => {
        document.querySelectorAll('.pct-mpanel').forEach(p => p.style.display = 'none');
    });
}

// Render a chart for every metric checked in the active dropdown, stacked.
function renderPctCharts(groups) {
    const area = document.getElementById('pctChartsArea');
    if (!area) return;
    if (!pctMetricType) {
        area.innerHTML = `<div class="chart-empty">No metrics available for this scenario.</div>`;
        return;
    }
    const group = groups[pctMetricType];
    const sel = pctMetricSel[pctMetricType] || new Set();
    const names = group.names.filter(n => sel.has(n));
    if (!names.length) {
        area.innerHTML = `<div class="chart-empty">No metrics selected. Check one or more metrics in the <strong>${escapeHtml(PCT_TYPE_LABEL[pctMetricType])}</strong> dropdown above.</div>`;
        return;
    }

    // Consistent combo colors across every stacked chart.
    const allRows = [];
    names.forEach(n => (group.byName[n] || []).forEach(r => allRows.push(r)));
    buildComboColorMap(allRows);

    const showTitles = names.length > 1;
    area.innerHTML = names.map((n, i) => `
        <div class="pct-block">
            ${showTitles ? `<h4 class="pct-block-title">${escapeHtml(n)}</h4>` : ''}
            <div id="pctChart_${i}" class="pct-chart"></div>
            <div id="pctStats_${i}" class="pct-stats"></div>
        </div>`).join('');

    names.forEach((n, i) => {
        drawPercentileChart(group.byName[n],
            document.getElementById('pctChart_' + i),
            document.getElementById('pctStats_' + i));
    });
}

function drawPercentileChart(rows, chartEl, statsEl) {
    if (!chartEl) return;
    const unit = rows[0].Unit && rows[0].Unit !== 'N/A' ? rows[0].Unit : '';
    const unitSuffix = unit ? ' ' + unit : '';

    // One series per Device + RAM config, each its own distinct color.
    const series = {};
    rows.forEach(r => {
        const key = r.Device + '\u0001' + r.Ram;
        (series[key] = series[key] || { device: r.Device, ram: r.Ram, values: [] }).values.push(Number(r.Value));
    });
    const keys = Object.keys(series);

    // Only show RAM in the legend if configs actually differ.
    const showRam = Array.from(new Set(rows.map(r => r.Ram))).length > 1;

    const traces = [];
    const drawn = [];   // retains per-series {name, color, values} for P50/P70/P90 markers
    keys.forEach(key => {
        const s = series[key];
        const values = s.values.filter(Number.isFinite).sort((a, b) => a - b);
        if (!values.length) return;
        const n = values.length;
        const color = colorForCombo(s.device, s.ram);
        const name = s.device + (showRam ? ` (${s.ram} GB)` : '');
        // With a single sample the distribution is degenerate (every percentile
        // equals that value), so draw a flat line spanning the whole axis rather
        // than a lone point. Otherwise spread the sorted values across 0–100.
        let xs, ys;
        if (n === 1) { xs = [0, 100]; ys = [values[0], values[0]]; }
        else { xs = values.map((_, i) => (i / (n - 1)) * 100); ys = values; }
        traces.push({
            type: 'scatter',
            mode: 'lines+markers',
            name: name,
            x: xs,
            y: ys,
            line: { color: color, width: 2.5, dash: 'solid' },
            marker: { color: color, size: 7 },
            hovertemplate: name + '<br>Percentile %{x:.0f}<br>Value %{y}' + unitSuffix + '<extra></extra>',
        });
        drawn.push({ name, color, values });
    });

    if (!traces.length) {
        chartEl.innerHTML = '<div class="chart-empty">No numeric values for this metric.</div>';
        if (statsEl) statsEl.innerHTML = '';
        return;
    }

    // Shared dashed vertical reference lines at P50 / P70 / P90.
    const refs = [50, 70, 90];
    const refColors = { 50: '#8B0000', 70: '#E67E00', 90: '#1a237e' };
    const refSymbols = { 50: 'diamond', 70: 'circle', 90: 'square' };
    const shapes = refs.map(p => ({ type: 'line', x0: p, x1: p, yref: 'paper', y0: 0, y1: 1,
        line: { color: refColors[p], width: 1, dash: 'dash' } }));

    // Highlight each line's P50 / P70 / P90 value with a symbol marker plus a
    // value label, both colored to match the line.
    const annotations = [];
    refs.forEach(p => {
        annotations.push({ x: p, y: 0, yref: 'paper', yanchor: 'top', ay: 18, ax: 0,
            text: `P${p}`, showarrow: false,
            bgcolor: refColors[p], font: { color: 'white', size: 11 }, bordercolor: refColors[p],
            borderpad: 3 });
    });

    // Overall value extent, used to decide when two series' values at the same
    // percentile are "close enough" that their labels would collide.
    let yMin = Infinity, yMax = -Infinity;
    drawn.forEach(d => d.values.forEach(v => { if (v < yMin) yMin = v; if (v > yMax) yMax = v; }));
    const yRange = (Number.isFinite(yMax) && yMax > yMin) ? (yMax - yMin) : 1;
    const closeThresh = yRange * 0.10;

    refs.forEach(p => {
        const markerTrace = { type: 'scatter', mode: 'markers', x: [], y: [], showlegend: false,
            marker: { color: [], size: 12, symbol: refSymbols[p], line: { color: '#333', width: 1 } },
            hovertemplate: 'P' + p + '<br>%{y:.1f}' + unitSuffix + '<extra></extra>' };

        // Collect each series' value at this percentile, sorted ascending, then
        // group values that sit within closeThresh of each other into clusters.
        const pts = drawn.map(d => ({ d, v: valueAtPercentile(d.values, p) }))
            .filter(o => Number.isFinite(o.v))
            .sort((a, b) => a.v - b.v);
        const clusters = [];
        pts.forEach(o => {
            const cur = clusters[clusters.length - 1];
            if (cur && (o.v - cur[cur.length - 1].v) < closeThresh) cur.push(o);
            else clusters.push([o]);
        });

        clusters.forEach(cl => {
            const k = cl.length;
            cl.forEach((o, i) => {
                markerTrace.x.push(p); markerTrace.y.push(o.v); markerTrace.marker.color.push(o.d.color);
                // A lone value keeps the simple label above its marker. Within a
                // crowded cluster, fan the labels out vertically (lower values
                // below the marker, higher values above) so they never overlap.
                const ay = (k === 1) ? -24 : -((i - (k - 1) / 2) * 30) - 4;
                annotations.push({ x: p, y: o.v, text: `${fmtNum(o.v)}${unitSuffix}`,
                    showarrow: true, arrowhead: 0, arrowcolor: o.d.color, ax: 0, ay: ay, standoff: 6,
                    bgcolor: o.d.color, font: { color: 'white', size: 10 }, bordercolor: o.d.color,
                    borderpad: 2 });
            });
        });
        if (markerTrace.x.length) traces.push(markerTrace);
    });

    const layout = {
        height: 460,
        margin: { l: 70, r: 30, t: 20, b: 70 },
        hovermode: 'closest',
        xaxis: { title: 'Percentile', range: [-2, 102], dtick: 10, gridcolor: '#eee', zeroline: false },
        yaxis: { title: unit ? `Value (${unit})` : 'Value', gridcolor: '#eee', zeroline: false },
        shapes: shapes,
        annotations: annotations,
        showlegend: true,
        legend: { orientation: 'h', y: 1.08, x: 0 },
        font: { family: 'Segoe UI, Tahoma, sans-serif', size: 12 },
        paper_bgcolor: 'white',
        plot_bgcolor: 'white',
    };

    Plotly.newPlot(chartEl, traces, layout, { responsive: true, displayModeBar: false });

    // Per-series summary table: P50 / P70 / P90 and the delta (P90 - P50).
    if (statsEl) {
        const rowsHtml = drawn.map(d => {
            const p50 = valueAtPercentile(d.values, 50);
            const p70 = valueAtPercentile(d.values, 70);
            const p90 = valueAtPercentile(d.values, 90);
            const delta = (Number.isFinite(p90) && Number.isFinite(p50)) ? p90 - p50 : NaN;
            return `<tr>
                <td><span class="pct-swatch" style="background:${d.color}"></span>${escapeHtml(d.name)}</td>
                <td>${fmtNum(p50)}${unitSuffix}</td>
                <td>${fmtNum(p70)}${unitSuffix}</td>
                <td>${fmtNum(p90)}${unitSuffix}</td>
                <td><strong>${fmtNum(delta)}${unitSuffix}</strong></td>
            </tr>`;
        }).join('');
        statsEl.innerHTML = `
            <table class="pct-stats-table">
                <thead><tr>
                    <th>Device / Config</th><th>P50</th><th>P70</th><th>P90</th><th>Delta (P90−P50)</th>
                </tr></thead>
                <tbody>${rowsHtml}</tbody>
            </table>`;
    }
}
