// ── Shared dashboard infrastructure ─────────────────────────────
//
// Loaded first. Owns DOM references, the filter bar (multi-selects + dates +
// last-N), data fetching, cross-view state, generic helpers, and the bootstrap
// that wires up event listeners. Each viewer (table.js / box.js /
// percentile.js) defines its own render function which renderCurrentView()
// dispatches to.

const startDate = document.getElementById('startDate');
const endDate = document.getElementById('endDate');
const lastN = document.getElementById('lastN');
const applyBtn = document.getElementById('applyBtn');
const resetBtn = document.getElementById('resetBtn');
const viewSelect = document.getElementById('viewSelect');
const resultsDiv = document.getElementById('results');
const metricModal = document.getElementById('metricModal');
const modalTitle = document.getElementById('modalTitle');
const modalChart = document.getElementById('modalChart');
const modalClose = document.getElementById('modalClose');

// ── Multi-select checkbox dropdown ──────────────────────────────
// Lightweight component: a toggle button showing a summary, plus a panel
// with "Select all" / "Clear" links and one checkbox per option.
const _allMultiSelects = [];
function createMultiSelect(container, opts) {
    opts = opts || {};
    const allLabel = opts.allLabel || 'All';
    const formatLabel = opts.formatLabel || (v => String(v));
    let values = [];
    let single = false;  // when true, only one option can be selected at a time
    const selected = new Set();

    const toggle = document.createElement('button');
    toggle.type = 'button';
    toggle.className = 'ms-toggle';
    const text = document.createElement('span');
    text.className = 'ms-text';
    const caret = document.createElement('span');
    caret.className = 'ms-caret';
    caret.textContent = '\u25be';
    toggle.appendChild(text);
    toggle.appendChild(caret);

    const panel = document.createElement('div');
    panel.className = 'ms-panel';
    panel.style.display = 'none';

    const actions = document.createElement('div');
    actions.className = 'ms-actions';
    const selAll = document.createElement('a');
    selAll.href = '#'; selAll.textContent = 'Select all';
    const clr = document.createElement('a');
    clr.href = '#'; clr.textContent = 'Clear';
    actions.appendChild(selAll);
    actions.appendChild(clr);
    panel.appendChild(actions);

    const list = document.createElement('div');
    list.className = 'ms-list';
    panel.appendChild(list);

    container.appendChild(toggle);
    container.appendChild(panel);

    // Keep the panel open while interacting with it: stop clicks inside from
    // reaching the document-level "close on outside click" handler.
    panel.addEventListener('click', e => e.stopPropagation());

    function updateText() {
        if (selected.size === 0 || selected.size === values.length) {
            text.textContent = `${allLabel} (${values.length})`;
        } else if (selected.size === 1) {
            text.textContent = formatLabel(Array.from(selected)[0]);
        } else {
            text.textContent = `${selected.size} selected`;
        }
    }

    function render() {
        list.innerHTML = '';
        values.forEach(v => {
            const item = document.createElement('label');
            item.className = 'ms-item';
            const cb = document.createElement('input');
            cb.type = 'checkbox';
            cb.checked = selected.has(v);
            cb.addEventListener('change', () => {
                if (single) {
                    // Radio-like: checking one clears the others.
                    selected.clear();
                    if (cb.checked) selected.add(v);
                    render();
                } else {
                    if (cb.checked) selected.add(v); else selected.delete(v);
                    updateText();
                }
            });
            const span = document.createElement('span');
            span.textContent = formatLabel(v);
            item.appendChild(cb);
            item.appendChild(span);
            list.appendChild(item);
        });
        updateText();
    }

    toggle.addEventListener('click', e => {
        e.stopPropagation();
        const open = panel.style.display !== 'none';
        _allMultiSelects.forEach(ms => ms._close());
        panel.style.display = open ? 'none' : 'block';
    });
    selAll.addEventListener('click', e => {
        e.preventDefault();
        values.forEach(v => selected.add(v));
        render();
    });
    clr.addEventListener('click', e => {
        e.preventDefault();
        selected.clear();
        render();
    });

    const api = {
        setOptions(items) {
            values = (items || []).map(String);
            selected.clear();
            render();
        },
        getSelected() { return Array.from(selected); },
        clear() { selected.clear(); render(); },
        // Toggle single-selection (radio-like) mode. When enabling, collapse any
        // existing multi-selection down to its first value.
        setSingle(flag) {
            single = !!flag;
            if (single && selected.size > 1) {
                const first = Array.from(selected)[0];
                selected.clear();
                selected.add(first);
            }
            selAll.style.display = single ? 'none' : '';
            render();
        },
        _close() { panel.style.display = 'none'; },
    };
    _allMultiSelects.push(api);
    return api;
}

const deviceMS = createMultiSelect(document.getElementById('deviceMS'), { allLabel: 'All devices' });
const ramMS = createMultiSelect(document.getElementById('ramMS'), { allLabel: 'All RAM configs', formatLabel: v => `${v} GB` });
const scenarioMS = createMultiSelect(document.getElementById('scenarioMS'), { allLabel: 'All scenarios' });
const hostMS = createMultiSelect(document.getElementById('hostMS'), { allLabel: 'All hosts' });

// Close any open multi-select panel when clicking outside.
document.addEventListener('click', () => { _allMultiSelects.forEach(ms => ms._close()); });

// ── Cross-view state ────────────────────────────────────────────
// Last fetched metrics, so switching views doesn't require a refetch.
let currentMetrics = null;
let currentTable = null;   // per-iteration columns for the transposed table views
let lastParams = '';

// Remembers the metric selection in the Percentile Distribution view so it
// survives re-renders (e.g. switching metrics without refetching). pctMetricType
// is which of the three metric-type dropdowns is active ('perf'|'calc'|'rail').
// pctMetricSel maps each type to a Set of the metric names currently checked in
// that dropdown's multi-select; the active type defaults to all of its metrics.
let pctMetricType = null;
let pctMetricSel = {};

// Stable device→color map so a device keeps the same color in the main
// box charts, the legend, and the zoomed-in modal.
const DEVICE_COLORS = ['#1f77b4', '#ff7f0e', '#2ca02c', '#d62728', '#9467bd', '#8c564b'];
let deviceColorMap = {};

function buildDeviceColorMap(metrics) {
    const devices = Array.from(new Set(metrics.map(m => m.Device))).sort();
    deviceColorMap = {};
    devices.forEach((d, i) => { deviceColorMap[d] = DEVICE_COLORS[i % DEVICE_COLORS.length]; });
}

function colorFor(device) {
    return deviceColorMap[device] || DEVICE_COLORS[0];
}

// ── Helpers ─────────────────────────────────────────────────────

function showStatus(msg, isError = false) {
    resultsDiv.innerHTML = `<div class="status ${isError ? 'error' : ''}">${msg}</div>`;
}

function showLoading(msg = 'Loading...') {
    resultsDiv.innerHTML = `<div class="status"><div class="spinner"></div><br>${msg}</div>`;
}

async function fetchJson(url) {
    const resp = await fetch(url);
    if (!resp.ok) {
        let detail = `Request failed: ${resp.status}`;
        try { const j = await resp.json(); if (j && j.error) detail = j.error; } catch (e) {}
        throw new Error(detail);
    }
    return resp.json();
}

function escapeHtml(value) {
    return String(value).replace(/[&<>"']/g, c => ({
        '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
    }[c]));
}

function fmtNum(v) {
    if (v === null || v === undefined || v === '' || Number.isNaN(v)) return '';
    if (typeof v !== 'number') return escapeHtml(String(v));
    return Number.isInteger(v) ? String(v) : (Math.abs(v) >= 100 ? v.toFixed(1) : v.toFixed(3));
}

// Linear-interpolated value at percentile p (0–100) of an ascending array.
// Shared by the table's P50/P70/P90 columns and the Percentile view.
function valueAtPercentile(sorted, p) {
    const n = sorted.length;
    if (n === 0) return null;
    if (n === 1) return sorted[0];
    const pos = (p / 100) * (n - 1);
    const lo = Math.floor(pos), hi = Math.ceil(pos);
    if (lo === hi) return sorted[lo];
    return sorted[lo] + (pos - lo) * (sorted[hi] - sorted[lo]);
}

// ── Load filter options on page load ───────────────────────────

async function loadFilters() {
    try {
        const opts = await fetchJson('/api/filters');
        if (opts.error) {
            showStatus('Error loading filters: ' + opts.error, true);
            return;
        }
        deviceMS.setOptions(opts.devices || []);
        ramMS.setOptions(opts.rams || []);
        scenarioMS.setOptions(opts.scenarios || []);
        hostMS.setOptions(opts.hostnames || []);
        // Constrain the date pickers to the available range and default to it.
        const dates = (opts.dates || []).slice().sort();  // ascending
        if (dates.length) {
            const min = dates[0];
            const max = dates[dates.length - 1];
            startDate.min = min; startDate.max = max; startDate.value = min;
            endDate.min = min; endDate.max = max; endDate.value = max;
        }
    } catch (e) {
        showStatus('Failed to load filters: ' + e.message, true);
    }
}

// ── Apply / Reset ──────────────────────────────────────────────

async function applyFilters() {
    const useLastN = lastN.value && Number(lastN.value) > 0;
    if (!useLastN && startDate.value && endDate.value && startDate.value > endDate.value) {
        showStatus('Start date must be on or before end date.', true);
        return;
    }
    const params = new URLSearchParams();
    deviceMS.getSelected().forEach(v => params.append('device', v));
    ramMS.getSelected().forEach(v => params.append('ram', v));
    scenarioMS.getSelected().forEach(v => params.append('scenario', v));
    hostMS.getSelected().forEach(v => params.append('host', v));
    // Date range and Last-N are mutually exclusive: Last-N takes precedence.
    if (useLastN) {
        params.set('last_n', lastN.value);
    } else {
        if (startDate.value) params.set('start_date', startDate.value);
        if (endDate.value) params.set('end_date', endDate.value);
    }

    showLoading('Fetching metrics...');
    lastParams = params.toString();
    try {
        const [metrics, table] = await Promise.all([
            fetchJson('/api/metrics?' + lastParams),
            fetchJson('/api/table?' + lastParams),
        ]);
        if (metrics.error) { showStatus('Error loading metrics: ' + metrics.error, true); return; }
        currentMetrics = (metrics && metrics.length) ? metrics : null;
        currentTable = (table && table.length) ? table : null;
        if (!currentMetrics && !currentTable) {
            showStatus('No data found for the selected filters.');
            return;
        }
        renderCurrentView();
    } catch (e) {
        showStatus('Failed to load data: ' + e.message, true);
    }
}

// Re-render the already-fetched data in whichever view is selected.
// The render functions live in table.js / box.js / percentile.js.
function renderCurrentView() {
    const v = viewSelect.value;
    if (v === 'box') {
        if (currentMetrics) renderBoxplots(currentMetrics);
    } else if (v === 'percentile') {
        if (currentMetrics) renderPercentile(currentMetrics);
    } else if (v === 'table_all') {
        if (currentTable) renderTableTransposed(currentTable, false);
        else showStatus('No data found for the selected filters.');
    } else {
        if (currentTable) renderTableTransposed(currentTable, true);
        else showStatus('No data found for the selected filters.');
    }
}

function resetFilters() {
    deviceMS.clear();
    ramMS.clear();
    scenarioMS.clear();
    hostMS.clear();
    startDate.value = startDate.min || '';
    endDate.value = endDate.max || '';
    lastN.value = '';
    syncExclusiveFilters();
    updateScenarioMode();
    pctMetricSel = {};
    pctMetricType = null;
    currentMetrics = null;
    showStatus('Choose any filters (or none) and click <strong>Apply</strong> to view metrics.');
}

function activeFilterSummary() {
    const parts = [];
    const dev = deviceMS.getSelected();
    const ram = ramMS.getSelected();
    const scn = scenarioMS.getSelected();
    const host = hostMS.getSelected();
    parts.push(`Device: <strong>${dev.length ? escapeHtml(dev.join(', ')) : 'All'}</strong>`);
    parts.push(`RAM: <strong>${ram.length ? escapeHtml(ram.map(r => r + ' GB').join(', ')) : 'All'}</strong>`);
    parts.push(`Scenario: <strong>${scn.length ? escapeHtml(scn.join(', ')) : 'All'}</strong>`);
    parts.push(`Host: <strong>${host.length ? escapeHtml(host.join(', ')) : 'All'}</strong>`);
    const useLastN = lastN.value && Number(lastN.value) > 0;
    if (useLastN) {
        parts.push(`Dates: <strong>All</strong>`);
        parts.push(`Last <strong>${escapeHtml(lastN.value)}</strong> iter.`);
    } else {
        const range = (startDate.value || endDate.value)
            ? `${escapeHtml(startDate.value || '…')} → ${escapeHtml(endDate.value || '…')}`
            : 'All';
        parts.push(`Dates: <strong>${range}</strong>`);
    }
    return parts.join(' &nbsp;|&nbsp; ');
}

// ── Data-source control (top-right indicator + modal) ──────────
const dsIndicator = document.getElementById('dsIndicator');
const dsLabel = document.getElementById('dsLabel');
const dsModal = document.getElementById('dsModal');
const dsClose = document.getElementById('dsClose');
const dsApply = document.getElementById('dsApply');
const dsJsonPanel = document.getElementById('dsJsonPanel');
const dsDrop = document.getElementById('dsDrop');
const dsFileInput = document.getElementById('dsFileInput');
const dsCount = document.getElementById('dsCount');
const dsClearBtn = document.getElementById('dsClearBtn');
const dsMsg = document.getElementById('dsMsg');

let dsState = { source: 'json', kusto_available: false, json_count: 0 };

function dsSourceLabel(src) {
    return src === 'kusto' ? 'Fungates (Kusto)' : 'JSON files';
}

function updateDsIndicator() {
    dsLabel.textContent = dsSourceLabel(dsState.source);
    dsIndicator.classList.toggle('ds-kusto', dsState.source === 'kusto');
    dsIndicator.classList.toggle('ds-json', dsState.source === 'json');
}

function selectedDsRadio() {
    const r = document.querySelector('input[name=dsSource]:checked');
    return r ? r.value : dsState.source;
}

function updateDsCount() {
    const n = dsState.json_count || 0;
    dsCount.textContent = n ? `${n} JSON file${n === 1 ? '' : 's'} uploaded.` : 'No files uploaded yet.';
}

function applyDsToModal() {
    document.querySelectorAll('input[name=dsSource]').forEach(r => {
        r.checked = r.value === dsState.source;
        if (r.value === 'kusto') r.disabled = !dsState.kusto_available;
    });
    dsJsonPanel.hidden = (selectedDsRadio() !== 'json');
    updateDsCount();
}

function showDsMsg(text, isErr) {
    dsMsg.textContent = text || '';
    dsMsg.classList.toggle('is-error', !!isErr);
    dsMsg.hidden = !text;
}

async function loadDataSource() {
    try {
        const d = await fetchJson('/api/datasource');
        if (!d.error) { dsState = d; updateDsIndicator(); }
    } catch (e) { /* keep default badge */ }
}

function openDsModal() { showDsMsg(''); applyDsToModal(); dsModal.hidden = false; }
function closeDsModal() { dsModal.hidden = true; }

async function uploadDsFiles(fileList) {
    const files = Array.from(fileList || []).filter(f => f.name.toLowerCase().endsWith('.json'));
    if (!files.length) { showDsMsg('Please choose .json files.', true); return; }
    const fd = new FormData();
    files.forEach(f => fd.append('files', f));
    showDsMsg('Uploading…');
    try {
        const res = await fetch('/api/datasource/upload', { method: 'POST', body: fd });
        const d = await res.json();
        dsState.json_count = d.total;
        updateDsCount();
        showDsMsg(`${d.saved} file(s) uploaded.${d.skipped ? ' ' + d.skipped + ' skipped (not .json).' : ''}`);
    } catch (e) {
        showDsMsg('Upload failed: ' + e.message, true);
    }
}

async function clearDsFiles() {
    try {
        const res = await fetch('/api/datasource/clear', { method: 'POST' });
        const d = await res.json();
        dsState.json_count = d.total;
        updateDsCount();
        showDsMsg('Uploaded files cleared.');
    } catch (e) { showDsMsg('Clear failed: ' + e.message, true); }
}

async function applyDataSource() {
    const src = selectedDsRadio();
    try {
        const res = await fetch('/api/datasource', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ source: src }),
        });
        const d = await res.json();
        if (d.error) { showDsMsg(d.error, true); return; }
        dsState = d;
        updateDsIndicator();
        closeDsModal();
        // The underlying dataset changed: reload filter options and reset the view.
        currentMetrics = null; currentTable = null; lastParams = '';
        await loadFilters();
        showStatus('Data source switched to <strong>' + escapeHtml(dsSourceLabel(d.source)) +
            '</strong>. Choose filters and click <strong>Apply</strong> to view metrics.');
    } catch (e) {
        showDsMsg('Failed to switch: ' + e.message, true);
    }
}

function initDataSource() {
    if (!dsIndicator) return;
    dsIndicator.addEventListener('click', openDsModal);
    dsClose.addEventListener('click', closeDsModal);
    dsModal.addEventListener('click', e => { if (e.target === dsModal) closeDsModal(); });
    dsApply.addEventListener('click', applyDataSource);
    document.querySelectorAll('input[name=dsSource]').forEach(r => {
        r.addEventListener('change', () => { dsJsonPanel.hidden = (selectedDsRadio() !== 'json'); showDsMsg(''); });
    });
    dsFileInput.addEventListener('change', () => uploadDsFiles(dsFileInput.files));
    dsClearBtn.addEventListener('click', clearDsFiles);
    ['dragenter', 'dragover'].forEach(ev => dsDrop.addEventListener(ev, e => { e.preventDefault(); dsDrop.classList.add('is-dragover'); }));
    ['dragleave', 'drop'].forEach(ev => dsDrop.addEventListener(ev, e => { e.preventDefault(); dsDrop.classList.remove('is-dragover'); }));
    dsDrop.addEventListener('drop', e => uploadDsFiles(e.dataTransfer.files));
    loadDataSource();
}

// ── Init ───────────────────────────────────────────────────────
// Date range and Last-N are mutually exclusive. Entering a Last-N value
// disables (greys out) the date pickers; clearing it re-enables them.
function syncExclusiveFilters() {
    const useLastN = lastN.value && Number(lastN.value) > 0;
    startDate.disabled = useLastN;
    endDate.disabled = useLastN;
    startDate.classList.toggle('disabled', useLastN);
    endDate.classList.toggle('disabled', useLastN);
}

// Some views (Box & Whisker, Percentile Distribution) describe a single
// scenario at a time, so the scenario filter becomes single-select while they
// are active. Multiple devices / RAM configs remain allowed (shown as separate
// colored series).
function updateScenarioMode() {
    const v = viewSelect.value;
    scenarioMS.setSingle(v === 'box' || v === 'percentile');
}

// Wire up event listeners once every viewer module has loaded and defined its
// globals (closeMetricModal lives in box.js, render*() in the viewer files).
document.addEventListener('DOMContentLoaded', () => {
    applyBtn.addEventListener('click', applyFilters);
    resetBtn.addEventListener('click', resetFilters);
    viewSelect.addEventListener('change', () => { updateScenarioMode(); renderCurrentView(); });
    lastN.addEventListener('input', syncExclusiveFilters);
    modalClose.addEventListener('click', closeMetricModal);
    metricModal.addEventListener('click', evt => { if (evt.target === metricModal) closeMetricModal(); });
    document.addEventListener('keydown', evt => {
        if (evt.key !== 'Escape') return;
        if (!metricModal.hidden) closeMetricModal();
        else if (dsModal && !dsModal.hidden) closeDsModal();
    });
    initDataSource();
    loadFilters();
});
