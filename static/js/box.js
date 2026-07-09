// ── Box & Whisker view (+ single-metric zoom modal) ─────────────────────────
//
// One box per metric, grouped by device. Clicking a box opens a zoomed modal
// with a per-device stats table.
//
// Depends on shared helpers from common.js: buildDeviceColorMap, colorFor,
// escapeHtml, activeFilterSummary, resultsDiv, metricModal, modalTitle.

const BOX_TYPES = [
    { type: 'PowerCalculation', title: 'Power Calculation', id: 'boxCalc' },
    { type: 'PowerMetrics', title: 'Power Metrics', id: 'boxPower' },
];

function median(values) {
    const s = values.slice().sort((a, b) => a - b);
    const mid = Math.floor(s.length / 2);
    return s.length % 2 ? s[mid] : (s[mid - 1] + s[mid]) / 2;
}

function renderBoxplots(metrics) {
    // The Box & Whisker view describes ONE scenario at a time. If the fetched
    // data spans several scenarios, prompt the user to narrow and re-Apply.
    const scenarios = Array.from(new Set(metrics.map(m => m.Scenario)));
    if (scenarios.length > 1) {
        resultsDiv.innerHTML =
            `<div class="results-summary">${activeFilterSummary()} &nbsp;|&nbsp; ${metrics.length} metric(s)</div>` +
            `<div class="chart-note">The Box &amp; Whisker view shows one scenario at a time. ` +
            `Please select a single <strong>Scenario</strong> and click <strong>Apply</strong> ` +
            `(currently ${scenarios.length} scenarios match).</div>`;
        return;
    }

    buildDeviceColorMap(metrics);
    let html = `<div class="results-summary">${activeFilterSummary()} &nbsp;|&nbsp; ${metrics.length} metric(s)</div>`;
    // Order: Performance Metrics first, then Power Calculation, then Power Metrics.
    // Curated performance metrics are derived in the same first / P50-P70-P90
    // manner as the Table (Specified) view.
    const perfRows = (typeof curatedPerfMetricRows === 'function' && currentTable)
        ? curatedPerfMetricRows(currentTable).filter(r => r.Scenario === scenarios[0])
        : [];
    html += `
        <div class="chart-card">
            <h3>Performance Metrics</h3>
            <div class="chart-sub">Curated performance metrics, shown the same way as the Table (Specified) view${'\u0020'}(first value, or P50/P70/P90). Box = quartiles, line = median.</div>
            <div class="box-holder"><div id="boxPerf"></div></div>
        </div>`;
    BOX_TYPES.forEach(cfg => {
        html += `
        <div class="chart-card">
            <h3>${cfg.title}</h3>
            <div class="chart-sub">Distribution of each metric across the selected iterations${'\u0020'}(box = quartiles, line = median).</div>
            <div class="box-holder"><div id="${cfg.id}"></div></div>
        </div>`;
    });
    resultsDiv.innerHTML = html;

    drawBoxChart('boxPerf', perfRows);
    BOX_TYPES.forEach(cfg => {
        const rows = metrics.filter(m => m.MetricType === cfg.type);
        drawBoxChart(cfg.id, rows);
    });
}

function drawBoxChart(elementId, rows) {
    const el = document.getElementById(elementId);
    if (!rows.length) {
        el.innerHTML = '<div class="chart-empty">No data for the selected filters.</div>';
        return;
    }

    // Label each metric row with its unit (units can differ within a type).
    const labelOf = r => r.Unit ? `${r.MetricName} (${r.Unit})` : r.MetricName;

    // Order metrics by overall median value ascending so the largest sit on top.
    const valuesByLabel = {};
    rows.forEach(r => {
        const v = Number(r.Value);
        if (!Number.isFinite(v)) return;
        (valuesByLabel[labelOf(r)] = valuesByLabel[labelOf(r)] || []).push(v);
    });
    const orderedLabels = Object.keys(valuesByLabel)
        .sort((a, b) => median(valuesByLabel[a]) - median(valuesByLabel[b]));

    // One box trace per device (color-coded) so multiple devices stay separate.
    const devices = Array.from(new Set(rows.map(r => r.Device)));
    const traces = devices.map(device => {
        const dr = rows.filter(r => r.Device === device);
        const color = colorFor(device);
        return {
            type: 'box',
            name: device,
            orientation: 'v',
            x: dr.map(labelOf),
            y: dr.map(r => Number(r.Value)),
            boxpoints: 'all',
            jitter: 0.4,
            pointpos: 0,
            marker: { size: 4, color: color },
            line: { color: color },
            fillcolor: color + '33',
            hovertemplate: '%{x}<br>%{y}<extra>' + device + '</extra>',
        };
    });

    const colWidth = Math.max(34, 60 - devices.length * 4);
    const width = Math.max(360, orderedLabels.length * devices.length * colWidth + 160);

    const layout = {
        width: width,
        height: 460,
        margin: { l: 64, r: 24, t: 10, b: 170 },
        boxmode: 'group',
        yaxis: { title: 'Value', zeroline: false, gridcolor: '#eee', automargin: true },
        xaxis: { categoryorder: 'array', categoryarray: orderedLabels, automargin: true, tickangle: -40 },
        legend: { orientation: 'h', y: 1.04, x: 0 },
        showlegend: devices.length > 1,
        font: { family: 'Segoe UI, Tahoma, sans-serif', size: 12 },
        paper_bgcolor: 'white',
        plot_bgcolor: 'white',
    };

    Plotly.newPlot(elementId, traces, layout, { responsive: false, displayModeBar: false });

    // Click a metric (box or point) to open a zoomed-in, auto-scaled view.
    el.on('plotly_click', evt => {
        if (!evt.points || !evt.points.length) return;
        const label = evt.points[0].x;
        openMetricModal(label, rows);
    });
}

// ── Single-metric zoom modal ───────────────────────────────────

function quantile(sorted, q) {
    const pos = (sorted.length - 1) * q;
    const base = Math.floor(pos);
    const rest = pos - base;
    return sorted[base + 1] !== undefined
        ? sorted[base] + rest * (sorted[base + 1] - sorted[base])
        : sorted[base];
}

function computeStats(values) {
    const s = values.slice().sort((a, b) => a - b);
    const n = s.length;
    const min = s[0], max = s[n - 1];
    const q1 = quantile(s, 0.25), med = quantile(s, 0.5), q3 = quantile(s, 0.75);
    const iqr = q3 - q1;
    const loBound = q1 - 1.5 * iqr, hiBound = q3 + 1.5 * iqr;
    const lowerWhisker = s.find(v => v >= loBound);
    let upperWhisker = max;
    for (const v of s) { if (v <= hiBound) upperWhisker = v; }
    const mean = s.reduce((a, b) => a + b, 0) / n;
    const variance = n > 1 ? s.reduce((a, b) => a + (b - mean) ** 2, 0) / (n - 1) : 0;
    const std = Math.sqrt(variance);
    return { n, min, lowerWhisker, q1, med, q3, upperWhisker, max, mean, std };
}

function fmt(v) {
    if (v === undefined || v === null || Number.isNaN(v)) return '—';
    return Number.isInteger(v) ? String(v) : v.toFixed(4);
}

function openMetricModal(label, typeRows) {
    const rows = typeRows.filter(r => (r.Unit ? `${r.MetricName} (${r.Unit})` : r.MetricName) === label);
    if (!rows.length) return;

    modalTitle.textContent = label;

    const devices = Array.from(new Set(rows.map(r => r.Device)));
    const unitLabel = rows[0].Unit ? ` ${rows[0].Unit}` : '';

    // One tall, vertical box per device. Hover shows each point's coordinate
    // (device + value); the chart is zoomable (scroll / drag / reset) so the
    // user can inspect devices whose values sit far apart on the shared axis.
    const traces = devices.map((device, i) => {
        const dr = rows.filter(r => r.Device === device);
        const color = colorFor(device);
        return {
            type: 'box',
            name: device,
            orientation: 'v',
            y: dr.map(r => Number(r.Value)),
            x: dr.map(() => device),
            boxmean: 'sd',
            boxpoints: 'all',
            jitter: 0.5,
            pointpos: 0,
            marker: { size: 6, color: color },
            line: { color: color },
            fillcolor: color + '33',
            hoveron: 'points',
            hovertemplate: '%{x}<br>Value: %{y}' + unitLabel + '<extra></extra>',
        };
    });

    const layout = {
        height: 460,
        margin: { l: 70, r: 30, t: 16, b: 70 },
        boxgap: 0.4,
        hovermode: 'closest',
        dragmode: 'zoom',
        yaxis: { title: 'Value' + (rows[0].Unit ? ` (${rows[0].Unit})` : ''), zeroline: false, gridcolor: '#eee', automargin: true },
        xaxis: { automargin: true, tickfont: { size: 12 } },
        showlegend: false,
        font: { family: 'Segoe UI, Tahoma, sans-serif', size: 12 },
        paper_bgcolor: 'white',
        plot_bgcolor: 'white',
    };

    // Enable interactive zoom: scroll-wheel zooms toward the cursor, drag boxes
    // a region, and the modebar / double-click resets the view.
    Plotly.newPlot('modalChart', traces, layout, {
        responsive: true,
        scrollZoom: true,
        displayModeBar: true,
        displaylogo: false,
        modeBarButtonsToRemove: ['lasso2d', 'select2d'],
    });

    // Build the stats table (one row per device).
    const unit = rows[0].Unit ? ` (${rows[0].Unit})` : '';
    let html = `<table><thead><tr>
        <th>Device</th><th>N</th><th>Min</th><th>Lower&nbsp;whisker</th>
        <th>Q1</th><th>Median</th><th>Q3</th><th>Upper&nbsp;whisker</th>
        <th>Max</th><th>Mean</th><th>Std&nbsp;dev</th></tr></thead><tbody>`;
    devices.forEach((device, i) => {
        const vals = rows.filter(r => r.Device === device).map(r => Number(r.Value)).filter(Number.isFinite);
        if (!vals.length) return;
        const st = computeStats(vals);
        const color = colorFor(device);
        html += `<tr>
            <td><span class="stats-swatch" style="background:${color}"></span>${escapeHtml(device)}</td>
            <td>${st.n}</td><td>${fmt(st.min)}</td><td>${fmt(st.lowerWhisker)}</td>
            <td>${fmt(st.q1)}</td><td>${fmt(st.med)}</td><td>${fmt(st.q3)}</td>
            <td>${fmt(st.upperWhisker)}</td><td>${fmt(st.max)}</td>
            <td>${fmt(st.mean)}</td><td>${fmt(st.std)}</td></tr>`;
    });
    html += `</tbody></table><div class="chart-sub" style="margin-top:8px">All values in${unit || ' raw units'}. &nbsp;Hover a point to read its value; scroll to zoom toward the cursor, drag to box-zoom, double-click to reset.</div>`;
    document.getElementById('modalStats').innerHTML = html;

    metricModal.hidden = false;
}

function closeMetricModal() {
    metricModal.hidden = true;
    Plotly.purge('modalChart');
}
