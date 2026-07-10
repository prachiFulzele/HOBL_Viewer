// ── Table view (Excel-style transposed: iterations as columns) ──────────────
//
// Rows = metrics/attributes, columns = iterations (grouped by scenario, then by
// date within a scenario). Two modes:
//   curated=true  -> only the user-specified metrics, with first / P50-P70-P90.
//   curated=false -> every metric (all power + all perf samples).
//
// Depends on shared helpers from common.js: escapeHtml, fmtNum,
// valueAtPercentile, activeFilterSummary, showStatus, resultsDiv.

// Curated perf metrics, in display order. `pts` = acceptable Perftrack Ids
// (match any). `kind`: 'first' shows the first matching sample; 'pct' shows
// P50/P70/P90 computed across that iteration's samples. `name` matches a
// no-pt perf entry (e.g. teams_fps). N/A when nothing matches in a column.
const CURATED_PERF = [
    { label: 'Office-Outlook-Boot v2', kind: 'first', pts: [8804] },
    { label: 'Office-Word-Boot v2', kind: 'first', pts: [8805] },
    { label: 'Office-XL-Boot v2', kind: 'first', pts: [8806] },
    { label: 'Office-PPT-Boot v2', kind: 'first', pts: [8807] },
    { label: 'Edge Launch (First Launch)', kind: 'first', pts: [8998] },
    { label: 'Edge Page Load', kind: 'first', pts: [9412] },
    { label: 'Start Launch', kind: 'pct', pts: [1809, 9917] },
    { label: 'File Explorer Launch', kind: 'first', pts: [9475, 10052] },
    { label: 'Snipping Tool Overlay Launch Performance', kind: 'first', pts: [9971] },
    { label: 'Type-to-Search', kind: 'pct', pts: [2430, 9459] },
    { label: 'Search Full Activation', kind: 'pct', pts: [9232] },
    { label: 'Explorer Input Delay', kind: 'first', pts: [6352] },
    { label: 'Teams FPS', kind: 'first', pts: [], name: 'teams_fps' },
    { label: 'Teams Video Resolution Width', kind: 'first', pts: [], name: 'teams_video_resolution_width' },
    { label: 'Teams Video Resolution Height', kind: 'first', pts: [], name: 'teams_video_resolution_height' },
];

// DUT header rows split into device-constant attributes (merged into a single
// cell per device) and variable attributes (shown per iteration column).
const CONST_LABELS = ['IHV', 'OEM', 'Device', 'Device name', 'HW Version (EV/DV)', 'Battery Capacity', 'Default RAM'];
const VAR_LABELS = ['LKG', 'OS Build', 'Usable RAM', 'Host Name'];

// Power Metrics section. The five defaults come from power_calculation; the user
// can add any other power_calculation metric or power_metrics rail via a picker.
const DEFAULT_POWER = [
    { name: 'soc_power', label: 'SoC Power (W)' },
    { name: 'memory_power', label: 'Memory Power (W)' },
    { name: 'display_power', label: 'Display Power (W)' },
    { name: 'system_power', label: 'System Power (W)' },
    { name: 'battery_life', label: 'Battery Life (hrs)' },
];
const DEFAULT_LABEL_BY_NAME = Object.fromEntries(DEFAULT_POWER.map(d => [d.name, d.label]));
const DEFAULT_POWER_KEYS = DEFAULT_POWER.map(d => 'calc:' + d.name);

// Selected power metrics: null = the five defaults; otherwise a Set of keys
// ('calc:<name>' / 'rail:<name>'). Persisted across re-renders so toggling the
// picker doesn't refetch. `powerPanelOpen` keeps the picker open across renders.
let powerSelection = null;
let powerPanelOpen = false;

// Gather every available power metric (de-duped, with its unit) from the columns.
function powerCatalog(cols) {
    const calc = [], rail = [], seenC = new Set(), seenR = new Set();
    const unitC = {}, unitR = {};
    cols.forEach(c => {
        Object.entries(c.PowerCalculation || {}).forEach(([n, e]) => {
            if (!seenC.has(n)) { seenC.add(n); calc.push(n); }
            if (e && e.unit && !unitC[n]) unitC[n] = e.unit;
        });
        Object.entries(c.PowerMetrics || {}).forEach(([n, e]) => {
            if (!seenR.has(n)) { seenR.add(n); rail.push(n); }
            if (e && e.unit && !unitR[n]) unitR[n] = e.unit;
        });
    });
    calc.sort(); rail.sort();
    return { calc, rail, unitC, unitR };
}
function calcLabel(name, unit) { return DEFAULT_LABEL_BY_NAME[name] || (unit ? `${name} (${unit})` : name); }
function railLabel(name, unit) { return unit ? `${name} (${unit})` : name; }

// The ordered list of power rows to display: defaults first (canonical order),
// then any other selected power_calculation metrics, then selected rails.
function activePowerRows(cat) {
    const sel = powerSelection || new Set(DEFAULT_POWER_KEYS);
    const rows = [];
    DEFAULT_POWER.forEach(d => {
        if (sel.has('calc:' + d.name) && cat.calc.includes(d.name)) {
            rows.push({ src: 'calc', name: d.name, label: d.label });
        }
    });
    cat.calc.forEach(n => {
        if (!DEFAULT_LABEL_BY_NAME[n] && sel.has('calc:' + n)) {
            rows.push({ src: 'calc', name: n, label: calcLabel(n, cat.unitC[n]) });
        }
    });
    cat.rail.forEach(n => {
        if (sel.has('rail:' + n)) rows.push({ src: 'rail', name: n, label: railLabel(n, cat.unitR[n]) });
    });
    return rows;
}

// The collapsible power-metric picker (defaults pre-checked).
function powerToolbarHtml(cat) {
    const sel = powerSelection || new Set(DEFAULT_POWER_KEYS);
    const item = (key, label, checked) =>
        `<label class="power-item"><input type="checkbox" value="${escapeHtml(key)}"${checked ? ' checked' : ''}>${escapeHtml(label)}</label>`;
    const calcItems = cat.calc.map(n => item('calc:' + n, calcLabel(n, cat.unitC[n]), sel.has('calc:' + n))).join('');
    const railItems = cat.rail.map(n => item('rail:' + n, railLabel(n, cat.unitR[n]), sel.has('rail:' + n))).join('');
    return `
        <div class="power-toolbar">
            <button type="button" id="powerToggle" class="power-btn">Power metrics shown \u25be</button>
            <div class="power-panel" id="powerPanel" style="display:${powerPanelOpen ? 'block' : 'none'}">
                <div class="power-group-title">Power Calculation</div>
                ${calcItems || '<div class="power-item">None</div>'}
                <div class="power-group-title">Power Metrics (rails)</div>
                ${railItems || '<div class="power-item">None</div>'}
            </div>
        </div>`;
}

// Wire the picker after render: toggle open/close, and re-render on selection.
function wirePowerToolbar() {
    const toggle = document.getElementById('powerToggle');
    const panel = document.getElementById('powerPanel');
    if (!toggle || !panel) return;
    toggle.addEventListener('click', e => {
        e.stopPropagation();
        powerPanelOpen = !powerPanelOpen;
        panel.style.display = powerPanelOpen ? 'block' : 'none';
    });
    panel.addEventListener('click', e => e.stopPropagation());
    panel.querySelectorAll('input[type=checkbox]').forEach(cb => {
        cb.addEventListener('change', () => {
            const checked = Array.from(panel.querySelectorAll('input[type=checkbox]:checked')).map(c => c.value);
            powerSelection = new Set(checked);
            renderCurrentView();
        });
    });
    if (!window._powerOutsideWired) {
        window._powerOutsideWired = true;
        document.addEventListener('click', () => {
            if (!powerPanelOpen) return;
            powerPanelOpen = false;
            const p = document.getElementById('powerPanel');
            if (p) p.style.display = 'none';
        });
    }
}

// Summary groups = one block of iterations sharing the same Device + config
// (Usable RAM) + Scenario. Each block gets 5 appended summary columns:
// P50 / P70 / P90 / CoV (coefficient of variation as a ratio) / P50–P90 Stretch
// ((P90−P50)/P50 as a percentage). Set by renderTableTransposed and consumed by
// the metric-row builders.
let _statGroups = [];        // [{ device, ram, scenario, cols: [...] }]
let _totalRenderCols = 0;    // iteration columns + 5 per summary group
let _metricRowIdx = 0;       // running metric-row counter (for zebra striping)

// The five computed summary cells for one group, given the metric's numeric
// samples across that group's iterations. P50/P70/P90 are percentiles of the
// samples; CoV is the coefficient of variation as a ratio (sample stddev /
// |mean|); Stretch is the P50→P90 spread as a percentage ((P90−P50)/|P50|).
function statValueCells(vals) {
    if (!vals.length) {
        return `<td class="stat na stat-start">N/A</td><td class="stat na">N/A</td><td class="stat na">N/A</td><td class="stat na">N/A</td><td class="stat na">N/A</td>`;
    }
    const sorted = vals.slice().sort((a, b) => a - b);
    const p50 = valueAtPercentile(sorted, 50);
    const p70 = valueAtPercentile(sorted, 70);
    const p90 = valueAtPercentile(sorted, 90);
    const mean = vals.reduce((a, b) => a + b, 0) / vals.length;
    let cov = 'N/A';
    if (vals.length > 1 && mean !== 0) {
        const variance = vals.reduce((a, b) => a + (b - mean) * (b - mean), 0) / (vals.length - 1);
        cov = (Math.sqrt(variance) / Math.abs(mean)).toFixed(3);
    }
    let stretch = 'N/A';
    if (Number.isFinite(p50) && Number.isFinite(p90) && p50 !== 0) {
        stretch = ((p90 - p50) / Math.abs(p50) * 100).toFixed(1) + '%';
    }
    return `<td class="stat num stat-start">${fmtNum(p50)}</td><td class="stat num">${fmtNum(p70)}</td><td class="stat num">${fmtNum(p90)}</td><td class="stat num cov">${cov}</td><td class="stat num stretch">${stretch}</td>`;
}

// Five blank summary cells (for header rows where stats don't apply).
function blankStatCells() {
    return `<td class="stat stat-start"></td><td class="stat"></td><td class="stat"></td><td class="stat"></td><td class="stat"></td>`;
}

// CoV heatmap colors: Excel's default 3-color scale — green (lowest variation),
// yellow (median), red (highest variation).
const _COV_GREEN = [0x63, 0xbe, 0x7b];
const _COV_YELLOW = [0xff, 0xeb, 0x84];
const _COV_RED = [0xf8, 0x69, 0x6b];

function _covMix(a, b, t) {
    const u = Math.max(0, Math.min(1, t));
    return `rgb(${Math.round(a[0] + (b[0] - a[0]) * u)}, ${Math.round(a[1] + (b[1] - a[1]) * u)}, ${Math.round(a[2] + (b[2] - a[2]) * u)})`;
}

// Map a CoV value to a color: min -> dark green, median -> yellow, max -> dark red.
// The yellow anchor sits at the median when it is strictly interior; otherwise it
// falls back to the value midpoint so the lowest stays green and the highest red.
function _covColor(v, min, med, max) {
    if (!(max > min)) return 'rgb(255, 235, 132)';
    let mid = med;
    if (!(mid > min && mid < max)) mid = (min + max) / 2;
    if (v <= mid) return _covMix(_COV_GREEN, _COV_YELLOW, (v - min) / (mid - min));
    return _covMix(_COV_YELLOW, _COV_RED, (v - mid) / (max - mid));
}

// Color the CoV cells as an Excel-style green->yellow->red 3-color scale,
// normalized independently per (section, group) — i.e. each metric section
// ("Power Metrics", "Performance Metrics", "Power Metrics (raw rails)") gets its
// own scale within each summary column, so power CoVs are never pooled with the
// generally larger perf CoVs.
//
// The bucket key is `sectionIdx:groupIdx`:
//   - sectionIdx increments at each `tr.section-row` while walking rows in order.
//   - groupIdx is derived by counting `.stat-start` cells (the P50 cell that
//     opens each group, present in both numeric and N/A rows) — NOT the position
//     among `td.cov` cells. N/A summary cells carry no `cov` class, so a
//     positional index would shift whenever a group is N/A for that row,
//     collapsing CoV cells from different scenario columns into the same bucket.
function applyCovHeatmap(root) {
    const cols = new Map();   // "sectionIdx:groupIdx" -> [{ cell, v }]
    let section = 0;
    root.querySelectorAll('table.xtable tr').forEach(row => {
        if (row.classList.contains('section-row')) { section++; return; }
        if (!row.classList.contains('metric-row')) return;
        let g = -1;
        row.querySelectorAll('td.stat').forEach(cell => {
            if (cell.classList.contains('stat-start')) g++;
            if (!cell.classList.contains('cov')) return;
            const v = parseFloat(cell.textContent);
            if (!Number.isFinite(v)) return;
            const key = section + ':' + g;
            if (!cols.has(key)) cols.set(key, []);
            cols.get(key).push({ cell, v });
        });
    });
    cols.forEach(bucket => {
        if (!bucket.length) return;
        const sorted = bucket.map(b => b.v).sort((a, b) => a - b);
        const min = sorted[0], max = sorted[sorted.length - 1];
        const med = valueAtPercentile(sorted, 50);
        bucket.forEach(({ cell, v }) => {
            cell.style.backgroundColor = _covColor(v, min, med, max);
            cell.classList.add('cov-heat');
        });
    });
}

// Generic metric data row: per-iteration cells (grouped) followed by the 4
// summary cells for each group. `cellFn(col)` -> { v, num } for one iteration
// cell; `sampleFn(col)` -> number[]|null contributing to that group's summary.
function metricRowHtml(label, cellFn, sampleFn, rowCls) {
    let h = `<td class="row-label">${escapeHtml(label)}</td>`;
    _statGroups.forEach((g, gi) => {
        g.cols.forEach((c, j) => {
            const r = cellFn(c);
            const cls = (r.num ? 'num' : 'na') + (gi > 0 && j === 0 ? ' grp-start' : '');
            h += `<td class="${cls}">${r.v}</td>`;
        });
        const vals = [];
        g.cols.forEach(c => {
            const s = sampleFn(c);
            if (s) for (const x of s) if (typeof x === 'number' && isFinite(x)) vals.push(x);
        });
        h += statValueCells(vals);
    });
    const cls = 'metric-row' + ((_metricRowIdx++ % 2) ? ' alt' : '') + (rowCls ? ' ' + rowCls : '');
    return `<tr class="${cls}">${h}</tr>`;
}

// Collect a curated perf metric's matching samples within one column.
function curatedSamples(col, cfg) {
    if (cfg.pts && cfg.pts.length) {
        for (const pt of cfg.pts) {
            const arr = col.PerfByPt[pt];
            if (arr && arr.length) return arr;
        }
    }
    if (cfg.name && col.PerfByName[cfg.name] !== undefined) return [col.PerfByName[cfg.name]];
    return null;
}

function renderTableTransposed(cols, curated) {
    if (!cols || !cols.length) { showStatus('No data found for the selected filters.'); return; }

    // Group columns into summary blocks by Device + config (Usable RAM) +
    // Scenario (contiguous, since the backend sorts Device->Ram->Scenario->...).
    // Each block also defines the scenario header merge and gets 4 summary cols.
    const scenarioGroups = [];
    const deviceGroups = [];
    cols.forEach(c => {
        let sg = scenarioGroups[scenarioGroups.length - 1];
        if (!sg || sg.scenario !== c.Scenario || sg.ram !== c.Ram || sg.device !== c.Device) {
            sg = { device: c.Device, ram: c.Ram, scenario: c.Scenario, cols: [] };
            scenarioGroups.push(sg);
        }
        sg.cols.push(c);

        // Device-constant attributes are merged per device (columns are sorted
        // by device, so each device's columns are contiguous).
        let vg = deviceGroups[deviceGroups.length - 1];
        if (!vg || vg.device !== c.Device) { vg = { device: c.Device, cols: [] }; deviceGroups.push(vg); }
        vg.cols.push(c);
    });
    const nCols = cols.length;

    // Each device's merged cells must also span that device's summary columns.
    deviceGroups.forEach(dg => {
        dg.statCount = scenarioGroups.filter(sg => sg.device === dg.device).length;
    });

    _statGroups = scenarioGroups;
    _totalRenderCols = nCols + 5 * scenarioGroups.length;
    _metricRowIdx = 0;

    const td = (s, cls) => `<td${cls ? ` class="${cls}"` : ''}>${s}</td>`;
    const labelCell = s => `<td class="row-label">${escapeHtml(s)}</td>`;

    let body = '';

    // Device-constant attributes: one merged cell per device (shown first), so
    // they are not repeated for every iteration but still align with the device's
    // columns (iterations + summary cols) in the main table.
    CONST_LABELS.forEach(lbl => {
        body += `<tr class="hdr-row">` + labelCell(lbl) +
            deviceGroups.map((g, gi) => {
                const val = escapeHtml(String(g.cols[0].Header[lbl] ?? ''));
                const span = g.cols.length + 5 * g.statCount;
                const cls = 'const-cell' + (gi > 0 ? ' grp-start' : '');
                return `<td class="${cls}" colspan="${span}"><span class="sticky-const">${val}</span></td>`;
            }).join('') + `</tr>`;
    });

    // Variable header rows: per iteration column (blank summary cells).
    VAR_LABELS.forEach(lbl => {
        let h = labelCell(lbl);
        scenarioGroups.forEach((g, gi) => {
            g.cols.forEach((c, j) => {
                h += `<td${gi > 0 && j === 0 ? ' class="grp-start"' : ''}>${escapeHtml(String(c.Header[lbl] ?? ''))}</td>`;
            });
            h += blankStatCells();
        });
        body += `<tr class="hdr-row">${h}</tr>`;
    });

    // Scenario row (merged per group; value pinned sticky like the const rows so it
    // stays left-aligned and visible until the next group scrolls in).
    body += `<tr class="scenario-row">` + `<td class="row-label">Scenario</td>` +
        scenarioGroups.map((g, gi) => `<td class="scenario-cell${gi > 0 ? ' grp-start' : ''}" colspan="${g.cols.length + 5}"><span class="sticky-const">${escapeHtml(g.scenario)}</span></td>`).join('') + `</tr>`;

    // Date row (merged per date within a group; value pinned sticky like the const
    // rows. Distinct dates within a group get a light/thin divider; blank summary
    // cell per group).
    {
        let h = `<td class="row-label">Date</td>`;
        scenarioGroups.forEach((g, gi) => {
            const subs = [];
            g.cols.forEach(c => {
                let d = subs[subs.length - 1];
                if (!d || d.date !== c.Date) { d = { date: c.Date, n: 0 }; subs.push(d); }
                d.n++;
            });
            subs.forEach((d, di) => {
                let cls = 'date-cell';
                if (gi > 0 && di === 0) cls += ' grp-start';
                else if (di > 0) cls += ' date-div';
                h += `<td class="${cls}" colspan="${d.n}"><span class="sticky-const">${escapeHtml(d.date)}</span></td>`;
            });
            h += `<td class="stat grp-start" colspan="5"></td>`;
        });
        body += `<tr class="date-row">${h}</tr>`;
    }

    // File Path row = run_number (blank summary cells).
    {
        let h = `<td class="row-label">File Path</td>`;
        scenarioGroups.forEach((g, gi) => {
            g.cols.forEach((c, j) => {
                h += `<td${gi > 0 && j === 0 ? ' class="grp-start"' : ''}>${escapeHtml(String(c.Iteration ?? ''))}</td>`;
            });
            h += blankStatCells();
        });
        body += `<tr class="filepath-row">${h}</tr>`;
    }

    // Iteration row = 1..N per group, with P50/P70/P90/CoV/Stretch summary headers.
    {
        let h = `<td class="row-label">Iteration</td>`;
        scenarioGroups.forEach((g, gi) => {
            g.cols.forEach((c, j) => {
                h += `<td${gi > 0 && j === 0 ? ' class="grp-start"' : ''}>${j + 1}</td>`;
            });
            h += `<td class="stat stat-hdr grp-start">P50</td><td class="stat stat-hdr">P70</td><td class="stat stat-hdr">P90</td><td class="stat stat-hdr">CoV</td><td class="stat stat-hdr stretch" title="P50\u2013P90 Stretch = (P90\u2212P50)/P50 \u00d7100%">P50\u2013P90 Stretch</td>`;
        });
        body += `<tr class="iter-row">${h}</tr>`;
    }

    // Section header helper: title text in a sticky span (the wide colspan cell
    // can't stick itself), so it stays visible while scrolling horizontally.
    const sectionRow = title =>
        `<tr class="section-row"><td colspan="${_totalRenderCols + 1}"><span class="section-title">${escapeHtml(title)}</span></td></tr>`;

    // Power Metrics section (selectable: defaults + any added calc/rail metrics).
    const cat = powerCatalog(cols);
    const powerRows = activePowerRows(cat);
    body += sectionRow('Power Metrics');
    powerRows.forEach(pr => {
        const get = c => (pr.src === 'calc' ? (c.PowerCalculation || {})[pr.name] : (c.PowerMetrics || {})[pr.name]);
        body += metricRowHtml(pr.label,
            c => {
                const e = get(c);
                const v = e ? e.value : null;
                const isNA = v === null || v === undefined || v === '';
                return { v: isNA ? 'N/A' : fmtNum(v), num: !isNA };
            },
            c => {
                const e = get(c);
                const v = e ? e.value : null;
                return (v === null || v === undefined || v === '') ? null : [Number(v)];
            });
    });

    // Performance Metrics section.
    body += sectionRow('Performance Metrics');
    body += curated ? curatedPerfRows(cols) : allPerfRows(cols);

    const toolbar = curated ? powerToolbarHtml(cat) : '';
    const html = `
    <div class="table-wrapper">
        <div class="results-summary">${activeFilterSummary()} &nbsp;|&nbsp; ${nCols} iteration(s)</div>
        ${toolbar}
        <div class="xtable-scroll">
            <table class="xtable">
                <tbody>${body}</tbody>
            </table>
        </div>
    </div>`;
    resultsDiv.innerHTML = html;
    if (curated) wirePowerToolbar();

    // The row-label column is sticky; its width varies with the longest label.
    // Pin each device-constant value just to the right of it so the value stays
    // visible while scrolling horizontally through that device's iterations.
    const labelCellEl = resultsDiv.querySelector('.xtable td.row-label');
    if (labelCellEl) {
        const w = Math.round(labelCellEl.getBoundingClientRect().width);
        resultsDiv.querySelectorAll('.sticky-const').forEach(s => { s.style.left = w + 'px'; });
    }

    // Color the CoV column as an Excel-style green->yellow->red 3-color scale
    // (low variation = green, median = yellow, high variation = red).
    applyCovHeatmap(resultsDiv);
}

// Curated perf rows: first-value or P50/P70/P90 (collapsed if all equal).
function curatedPerfRows(cols) {
    let out = '';

    CURATED_PERF.forEach(cfg => {
        if (cfg.kind === 'first') {
            out += metricRowHtml(cfg.label,
                c => { const s = curatedSamples(c, cfg); return { v: s ? fmtNum(s[0]) : 'N/A', num: !!s }; },
                c => { const s = curatedSamples(c, cfg); return s ? [s[0]] : null; });
        } else {
            // Per-column P50/P70/P90 (keyed by column for metricRowHtml lookup).
            const p50 = new Map(), p70 = new Map(), p90 = new Map();
            let equal = true;
            cols.forEach(c => {
                const s = curatedSamples(c, cfg);
                if (s) {
                    const sorted = s.slice().sort((a, b) => a - b);
                    const a = valueAtPercentile(sorted, 50), b = valueAtPercentile(sorted, 70), d = valueAtPercentile(sorted, 90);
                    p50.set(c, a); p70.set(c, b); p90.set(c, d);
                    if (a !== b || a !== d) equal = false;
                } else { p50.set(c, null); p70.set(c, null); p90.set(c, null); }
            });
            const rowFor = (label, map) => metricRowHtml(label,
                c => { const v = map.get(c); return { v: v === null || v === undefined ? 'N/A' : fmtNum(v), num: v !== null && v !== undefined }; },
                c => { const v = map.get(c); return (v === null || v === undefined) ? null : [v]; });
            if (equal) {
                out += rowFor(cfg.label, p50);
            } else {
                out += rowFor(cfg.label + ' P50', p50);
                out += rowFor(cfg.label + ' P70', p70);
                out += rowFor(cfg.label + ' P90', p90);
            }
        }
    });
    return out;
}

// All-metrics perf rows: every metric, all sample values listed per column.
function allPerfRows(cols) {
    // Union of all perf identities across columns. Key by pt when present,
    // else by name. Keep a friendly display name.
    const order = [];
    const seen = new Map();
    cols.forEach(c => {
        c.PerfAll.forEach(p => {
            const key = (p.pt !== null && p.pt !== undefined) ? 'pt:' + p.pt : 'nm:' + p.name;
            if (!seen.has(key)) {
                seen.set(key, { key, pt: p.pt, name: p.name });
                order.push(key);
            }
        });
    });

    const samplesFor = (c, meta) => {
        if (meta.pt != null) return c.PerfByPt[meta.pt] || null;
        return (c.PerfByName[meta.name] !== undefined) ? [c.PerfByName[meta.name]] : null;
    };

    let out = '';
    order.forEach(key => {
        const meta = seen.get(key);
        const label = meta.name + (meta.pt != null ? ` (pt ${meta.pt})` : '');
        out += metricRowHtml(label,
            c => { const v = samplesFor(c, meta); return { v: (v && v.length) ? v.map(fmtNum).join(', ') : 'N/A', num: !!(v && v.length) }; },
            c => samplesFor(c, meta));
    });

    // Also list all power_metrics (pm_emi_*) in the all view.
    const pmNames = [];
    const pmSeen = new Set();
    cols.forEach(c => Object.keys(c.PowerMetrics).forEach(n => { if (!pmSeen.has(n)) { pmSeen.add(n); pmNames.push(n); } }));
    if (pmNames.length) {
        out += `<tr class="section-row"><td colspan="${_totalRenderCols + 1}"><span class="section-title">Power Metrics (raw rails)</span></td></tr>`;
        pmNames.forEach(n => {
            out += metricRowHtml(n,
                c => { const e = c.PowerMetrics[n]; return { v: e ? fmtNum(e.value) : 'N/A', num: !!e }; },
                c => { const e = c.PowerMetrics[n]; return e ? [Number(e.value)] : null; });
        });
    }
    return out;
}

// Build synthetic per-iteration metric rows for the curated perf metrics, in the
// SAME first / P50-P70-P90 manner used by the Table (Specified) view. The output
// shape matches /api/metrics rows ({Device, Ram, Scenario, MetricName,
// MetricType:'PerfMetrics', Unit, Value}) so box.js and percentile.js can plot
// them like any other metric. Derived from table columns because PerfByPt
// preserves JSON order (needed for the "first value" rule).
function curatedPerfMetricRows(cols) {
    if (!cols || !cols.length) return [];
    const out = [];
    CURATED_PERF.forEach(cfg => {
        const unit = cfg.name === 'teams_fps' ? 'fps' : 'ms';
        const base = c => ({ Device: c.Device, Ram: c.Ram, Scenario: c.Scenario,
            MetricType: 'PerfMetrics', Unit: unit });

        if (cfg.kind === 'first') {
            cols.forEach(c => {
                const s = curatedSamples(c, cfg);
                if (s && Number.isFinite(s[0])) {
                    out.push(Object.assign(base(c), { MetricName: cfg.label, Value: s[0] }));
                }
            });
        } else {
            // P50/P70/P90 across each iteration's samples; collapse to a single
            // metric when all three coincide for every iteration (mirrors table).
            const recs = [];
            cols.forEach(c => {
                const s = curatedSamples(c, cfg);
                if (!s || !s.length) return;
                const sorted = s.slice().sort((a, b) => a - b);
                recs.push({ c,
                    p50: valueAtPercentile(sorted, 50),
                    p70: valueAtPercentile(sorted, 70),
                    p90: valueAtPercentile(sorted, 90) });
            });
            const equal = recs.every(r => r.p50 === r.p70 && r.p50 === r.p90);
            recs.forEach(r => {
                if (equal) {
                    out.push(Object.assign(base(r.c), { MetricName: cfg.label, Value: r.p50 }));
                } else {
                    out.push(Object.assign(base(r.c), { MetricName: cfg.label + ' P50', Value: r.p50 }));
                    out.push(Object.assign(base(r.c), { MetricName: cfg.label + ' P70', Value: r.p70 }));
                    out.push(Object.assign(base(r.c), { MetricName: cfg.label + ' P90', Value: r.p90 }));
                }
            });
        }
    });
    return out;
}