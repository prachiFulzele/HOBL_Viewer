const regDevice = document.getElementById('regDevice');
const regRam = document.getElementById('regRam');
const regScenario = document.getElementById('regScenario');
const regBuildToggle = document.getElementById('regBuildToggle');
const regBuildPanel = document.getElementById('regBuildPanel');
const regBuildOptions = document.getElementById('regBuildOptions');
const regBuildAll = document.getElementById('regBuildAll');
const regBuildClear = document.getElementById('regBuildClear');
const regMetricToggle = document.getElementById('regMetricToggle');
const regMetricPanel = document.getElementById('regMetricPanel');
const regMetricOptions = document.getElementById('regMetricOptions');
const regMetricSpecified = document.getElementById('regMetricSpecified');
const regMetricAll = document.getElementById('regMetricAll');
const regMetricClear = document.getElementById('regMetricClear');
const regDirection = document.getElementById('regDirection');
const regExcludeUnder8 = document.getElementById('regExcludeUnder8');
const regApply = document.getElementById('regApply');
const regReload = document.getElementById('regReload');
const regResults = document.getElementById('regressionResults');
let regRequest = 0;
let regInitialized = false;
let regBuildCatalog = [];
let regSelectedBuilds = new Set();
let regMetricCatalog = [];
let regSelectedMetrics = new Set();

function setRegressionView(active) {
    document.getElementById('regressionView').hidden = !active;
    document.getElementById('results').hidden = active;
    document.getElementById('dashboardFilters').classList.toggle('regression-view', active);
    if (active && !regInitialized) loadRegressionFilters();
    if (active) {
        document.querySelectorAll('[id^="regressionChart-"].js-plotly-plot').forEach(chart => {
            Plotly.Plots.resize(chart).catch(error => showRegressionChartError(chart, error));
        });
    }
}

function resetRegressionView() {
    ++regRequest;
    regInitialized = false;
    regApply.disabled = true;
    [regDevice, regRam, regScenario].forEach(select =>
        setRegressionOptions(select, [], 'Choose a value'));
    setRegressionBuilds([]);
    setRegressionMetrics([]);
    regressionStatus('Select filters to compare builds.');
    if (document.getElementById('viewSelect').value === 'regression') loadRegressionFilters();
}

function regressionStatus(message, error = false) {
    const status = document.createElement('div');
    status.className = `status${error ? ' error' : ''}`;
    status.textContent = message;
    clearRegressionResults();
    regResults.appendChild(status);
}

function clearRegressionResults() {
    if (typeof Plotly !== 'undefined') {
        regResults.querySelectorAll('[id^="regressionChart-"]').forEach(chart => Plotly.purge(chart));
    }
    regResults.replaceChildren();
}

function setRegressionOptions(select, options, placeholder) {
    select.replaceChildren(new Option(placeholder, ''));
    options.forEach(option => {
        const item = new Option(option.label, option.value);
        if (option.unit !== undefined) item.dataset.unit = option.unit;
        select.add(item);
    });
    select.disabled = !options.length;
}

function simpleRegressionOptions(values) {
    return values.map(value => ({ value: String(value), label: String(value) }));
}

function updateRegressionApplyState() {
    regApply.disabled = !regSelectedMetrics.size || !regSelectedBuilds.size;
}

function renderRegressionBuildOptions() {
    regBuildOptions.replaceChildren();
    regBuildCatalog.forEach(build => {
        const label = document.createElement('label');
        label.className = 'power-item';
        const checkbox = document.createElement('input');
        checkbox.type = 'checkbox';
        checkbox.value = build;
        checkbox.checked = regSelectedBuilds.has(build);
        checkbox.addEventListener('change', () => {
            if (checkbox.checked) regSelectedBuilds.add(build);
            else regSelectedBuilds.delete(build);
            updateRegressionBuildState();
        });
        label.append(checkbox, document.createTextNode(build));
        regBuildOptions.appendChild(label);
    });
}

function updateRegressionBuildState() {
    const count = regSelectedBuilds.size;
    regBuildToggle.textContent = count ? `${count} build${count === 1 ? '' : 's'} selected \u25be`
        : 'Choose builds \u25be';
    updateRegressionApplyState();
    if (regBuildCatalog.length) {
        ++regRequest;
        regressionStatus(count
            ? 'Build selections changed. Click Compare Builds to refresh results.'
            : 'Select at least one build to compare.');
    }
}

function setRegressionBuilds(builds, selectAll = false) {
    regBuildCatalog = builds || [];
    regSelectedBuilds = selectAll ? new Set(regBuildCatalog) : new Set();
    regBuildToggle.disabled = !regBuildCatalog.length;
    regBuildPanel.hidden = true;
    renderRegressionBuildOptions();
    regBuildToggle.textContent = regSelectedBuilds.size
        ? `${regSelectedBuilds.size} builds selected \u25be` : 'Choose builds \u25be';
    updateRegressionApplyState();
}

function selectRegressionBuilds(builds) {
    regSelectedBuilds = new Set(builds);
    renderRegressionBuildOptions();
    updateRegressionBuildState();
}

function specifiedRegressionMetricValues(options = regMetricCatalog) {
    const parsed = options.map(option => {
        try {
            const [source, name, pt] = JSON.parse(option.value);
            return { option, source, name, pt: pt === null ? null : String(pt) };
        } catch (_error) {
            return { option, source: '', name: '', pt: null };
        }
    });
    const defaultPowerNames = new Set(DEFAULT_POWER.map(metric => metric.name));
    const hasDefaultPower = parsed.some(item =>
        item.source === 'PowerCalculation' && defaultPowerNames.has(item.name));
    const tablePowerSelection = typeof powerSelection !== 'undefined' && powerSelection !== null
        ? powerSelection : null;
    const selected = new Set();
    parsed.forEach(item => {
        const tableKey = item.source === 'PowerCalculation' ? `calc:${item.name}`
            : item.source === 'PowerMetrics' ? `rail:${item.name}` : null;
        if (tablePowerSelection && tableKey && tablePowerSelection.has(tableKey)) {
            selected.add(item.option.value);
        } else if (!tablePowerSelection && item.source === 'PowerCalculation'
                && defaultPowerNames.has(item.name)) {
            selected.add(item.option.value);
        } else if (!tablePowerSelection && item.source === 'PowerMetrics' && !hasDefaultPower) {
            selected.add(item.option.value);
        } else if (item.source === 'PerfMetrics' && CURATED_PERF.some(metric =>
            (metric.name && metric.name === item.name)
            || (item.pt !== null && metric.pts.map(String).includes(item.pt)))) {
            selected.add(item.option.value);
        }
    });
    return selected;
}

function renderRegressionMetricOptions() {
    regMetricOptions.replaceChildren();
    regMetricCatalog.forEach(option => {
        const label = document.createElement('label');
        label.className = 'power-item';
        const checkbox = document.createElement('input');
        checkbox.type = 'checkbox';
        checkbox.value = option.value;
        checkbox.checked = regSelectedMetrics.has(option.value);
        checkbox.addEventListener('change', () => {
            if (checkbox.checked) regSelectedMetrics.add(option.value);
            else regSelectedMetrics.delete(option.value);
            updateRegressionMetricState();
        });
        label.append(checkbox, document.createTextNode(option.label));
        regMetricOptions.appendChild(label);
    });
}

function updateRegressionMetricState() {
    const count = regSelectedMetrics.size;
    regMetricToggle.textContent = count ? `${count} metric${count === 1 ? '' : 's'} selected \u25be`
        : 'Choose metrics \u25be';
    updateRegressionApplyState();
    if (regMetricCatalog.length) {
        ++regRequest;
        regressionStatus(count
            ? 'Metric selections changed. Click Compare Builds to refresh results.'
            : 'Select at least one metric to compare builds.');
    }
}

function setRegressionMetrics(options, selectSpecified = false) {
    regMetricCatalog = options || [];
    regSelectedMetrics = selectSpecified ? specifiedRegressionMetricValues(regMetricCatalog) : new Set();
    regMetricToggle.disabled = !regMetricCatalog.length;
    regMetricPanel.hidden = true;
    renderRegressionMetricOptions();
    regMetricToggle.textContent = regSelectedMetrics.size
        ? `${regSelectedMetrics.size} metrics selected \u25be` : 'Choose metrics \u25be';
    updateRegressionApplyState();
}

function selectRegressionMetrics(values) {
    regSelectedMetrics = new Set(values);
    renderRegressionMetricOptions();
    updateRegressionMetricState();
}

async function regressionFetch(url) {
    const response = await fetch(url);
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || `Request failed (${response.status})`);
    return data;
}

function regressionParams() {
    return new URLSearchParams({
        device: regDevice.value, ram: regRam.value, scenario: regScenario.value,
    });
}

async function loadRegressionFilters() {
    regInitialized = true;
    const request = ++regRequest;
    regApply.disabled = true;
    [regDevice, regRam, regScenario].forEach(select =>
        setRegressionOptions(select, [], 'Choose a value'));
    setRegressionBuilds([]);
    setRegressionMetrics([]);
    regressionStatus('Loading devices...');
    try {
        const filters = await regressionFetch('/api/filters');
        if (request !== regRequest) return;
        setRegressionOptions(regDevice, simpleRegressionOptions(filters.devices), 'Choose one device');
        regressionStatus(filters.devices.length
            ? 'Choose one device to load its RAM configurations.'
            : 'No devices are available. Configure the data source or upload JSON files on the Dashboard.');
    } catch (error) {
        if (request === regRequest) regressionStatus(`Unable to load filters: ${error.message}`, true);
    }
}

async function cascadeRegressionFilters(level) {
    const request = ++regRequest;
    regApply.disabled = true;
    const downstream = level === 'device' ? [regRam, regScenario]
        : level === 'ram' ? [regScenario] : [];
    downstream.forEach(select => setRegressionOptions(select, [], 'Choose a value'));
    setRegressionBuilds([]);
    setRegressionMetrics([]);
    regressionStatus('Select filters to compare builds.');
    const current = level === 'device' ? regDevice : level === 'ram' ? regRam : regScenario;
    if (!current.value) return;
    regressionStatus('Loading matching options...');
    try {
        const data = await regressionFetch(`/api/regression?${regressionParams()}`);
        if (request !== regRequest) return;
        if (level === 'scenario') {
            setRegressionBuilds(data.builds, true);
            setRegressionMetrics(data.metrics, true);
            regressionStatus(data.metrics.length
                ? `${regSelectedMetrics.size} Table (Specified) metrics and ${regSelectedBuilds.size} builds selected. Click Compare Builds or adjust either picker.`
                : 'No matching metrics are available for these filters.');
            if (regSelectedMetrics.size && regSelectedBuilds.size) await runRegressionComparison();
        } else {
            const select = level === 'device' ? regRam : regScenario;
            const options = level === 'device' ? simpleRegressionOptions(data.rams)
                : simpleRegressionOptions(data.scenarios);
            setRegressionOptions(select, options,
                `Choose one ${level === 'device' ? 'RAM configuration' : 'scenario'}`);
            regressionStatus(options.length ? 'Complete the selections to load metrics.'
                : 'No matching options are available for these filters.');
        }
    } catch (error) {
        if (request === regRequest) regressionStatus(`Unable to load options: ${error.message}`, true);
    }
}

function regressionNumber(value, signed = false) {
    if (value === null) return 'N/A';
    const text = Number(value).toLocaleString(undefined, { maximumSignificantDigits: 8 });
    return signed && value > 0 ? `+${text}` : text;
}

function renderRegression(data) {
    clearRegressionResults();
    const comparisons = data.comparisons || (data.rows ? [{
        metric: regMetricCatalog.find(option => regSelectedMetrics.has(option.value))
            || { label: 'Selected metric', unit: '' },
        rows: data.rows,
        excluded_builds: data.excluded_builds,
        missing_build_iterations: data.missing_build_iterations,
        invalid_samples: data.invalid_samples,
    }] : []);
    const summary = document.createElement('div');
    summary.className = 'results-summary';
    const regressions = comparisons.reduce((count, comparison) =>
        count + comparison.rows.filter(row => row.status === 'Regression').length, 0);
    summary.textContent = `${regDevice.value} / ${regRam.value} GB / ${regScenario.value} | ` +
        `${regSelectedBuilds.size} build(s) | ${comparisons.length} metric(s) | ` +
        `${regDirection.selectedOptions[0].textContent} | ` +
        `${regressions} regression(s)`;
    regResults.appendChild(summary);

    comparisons.forEach((comparison, index) => {
        renderRegressionComparison(comparison, index);
    });
}

function renderRegressionComparison(comparison, index) {
    const block = document.createElement('section');
    block.className = 'regression-metric-block';
    const heading = document.createElement('h3');
    heading.className = 'regression-metric-heading';
    heading.textContent = comparison.metric.label;
    block.appendChild(heading);
    const notes = [];
    if (regExcludeUnder8.checked) {
        notes.push(`${comparison.excluded_builds} build(s) excluded with fewer than 8 iterations containing valid samples. Changes compare the remaining builds.`);
        if (!comparison.rows.length) notes.push('No builds meet the 8-iteration minimum.');
    }
    if (comparison.rows.length < 2) notes.push('At least two builds are needed for a comparison.');
    if (comparison.missing_build_iterations) notes.push(`${comparison.missing_build_iterations} iteration(s) excluded because the build number is missing.`);
    if (comparison.invalid_samples) notes.push(`${comparison.invalid_samples} non-numeric or non-finite sample(s) excluded.`);
    if (notes.length) {
        const note = document.createElement('p');
        note.className = 'chart-note';
        note.textContent = notes.join(' ');
        block.appendChild(note);
    }
    regResults.appendChild(block);
    if (!comparison.rows.length) return;

    const wrapper = document.createElement('div');
    wrapper.className = 'table-wrapper review-table-scroll';
    const table = document.createElement('table');
    table.className = 'review-table';
    const head = table.createTHead().insertRow();
    ['Build', 'Previous build', 'P50', 'Samples', 'Iterations with metric', 'P50 change', 'Change (%)', 'Result']
        .forEach(label => {
            const cell = document.createElement('th');
            cell.textContent = label;
            head.appendChild(cell);
        });
    const body = table.createTBody();
    comparison.rows.forEach(row => {
        const tr = body.insertRow();
        [row.build, row.previous_build || 'N/A', regressionNumber(row.p50), row.samples,
            row.iterations, regressionNumber(row.delta, true),
            row.percent === null ? 'N/A' : `${regressionNumber(row.percent, true)}%`, row.status]
            .forEach(value => {
                tr.insertCell().textContent = value;
            });
        tr.lastElementChild.className = row.status === 'Regression' ? 'regression-worse'
            : row.status === 'Improvement' ? 'regression-better' : '';
    });
    wrapper.appendChild(table);
    block.appendChild(wrapper);
    renderRegressionChart(comparison.rows, wrapper, comparison.metric, index, block);
}

function showRegressionChartError(chart, error) {
    if (typeof Plotly !== 'undefined') Plotly.purge(chart);
    const message = document.createElement('div');
    message.className = 'status error';
    message.textContent = `Unable to plot P50 values: ${error.message}`;
    chart.replaceChildren(message);
}

async function renderRegressionChart(rows, table, metric, index, block) {
    const card = document.createElement('section');
    card.className = 'chart-card';
    const title = document.createElement('h3');
    title.textContent = `Build-wise P50 - ${metric.label}`;
    const note = document.createElement('p');
    note.className = 'chart-sub';
    note.textContent = 'Red = regression, green = improvement, blue = baseline, gray = unchanged or not comparable. Hover for P50 and build-to-build changes. Missing samples leave a gap.';
    const chart = document.createElement('div');
    chart.id = `regressionChart-${index}`;
    chart.setAttribute('role', 'img');
    chart.setAttribute('aria-label', title.textContent);
    card.append(title, note, chart);
    block.insertBefore(card, table);
    const builds = rows.map(row => row.build);
    const unit = metric.unit;
    try {
        if (typeof Plotly === 'undefined') throw new Error('The chart library could not be loaded. Reload the dashboard to try again.');
        await Plotly.newPlot(chart, [{
            type: 'scatter',
            mode: 'lines+markers',
            name: 'P50',
            x: builds,
            y: rows.map(row => row.p50),
            connectgaps: false,
            line: { color: '#0078d4', width: 2 },
            marker: {
                size: 10,
                color: rows.map(row => row.status === 'Regression' ? '#a4262c'
                    : row.status === 'Improvement' ? '#107c10'
                    : row.status === 'Baseline' ? '#0078d4' : '#605e5c'),
            },
            customdata: rows.map(row => [
                escapeHtml(row.build), row.status, row.samples, row.iterations,
                escapeHtml(row.previous_build || 'N/A'), regressionNumber(row.delta, true),
                row.percent === null ? 'N/A' : `${regressionNumber(row.percent, true)}%`,
            ]),
            hovertemplate: 'Build: %{customdata[0]}<br>P50: %{y}<br>Result: %{customdata[1]}' +
                '<br>Samples: %{customdata[2]}<br>Iterations: %{customdata[3]}' +
                '<br>Previous build: %{customdata[4]}<br>Change: %{customdata[5]}' +
                '<br>Change (%): %{customdata[6]}<extra></extra>',
        }], {
            height: 420,
            margin: { t: 25, r: 25, b: 100, l: 80 },
            xaxis: {
                title: 'Build',
                type: 'category',
                categoryorder: 'array',
                categoryarray: builds,
                tickmode: 'array',
                tickvals: builds,
                ticktext: builds.map(escapeHtml),
                tickangle: -35,
                automargin: true,
            },
            yaxis: { title: unit ? `P50 (${escapeHtml(unit)})` : 'P50', automargin: true },
            font: { family: 'Segoe UI, Tahoma, sans-serif', size: 12 },
            paper_bgcolor: 'white',
            plot_bgcolor: 'white',
            showlegend: false,
        }, { responsive: true, displayModeBar: true, displaylogo: false });
    } catch (error) {
        showRegressionChartError(chart, error);
    }
}

regDevice.addEventListener('change', () => cascadeRegressionFilters('device'));
regRam.addEventListener('change', () => cascadeRegressionFilters('ram'));
regScenario.addEventListener('change', () => cascadeRegressionFilters('scenario'));
[regDirection, regExcludeUnder8].forEach(select => select.addEventListener('change', () => {
    if (!regSelectedMetrics.size || !regSelectedBuilds.size) return;
    ++regRequest;
    regApply.disabled = false;
    regressionStatus('Selections changed. Click Compare Builds to refresh results.');
}));
regBuildToggle.addEventListener('click', event => {
    event.stopPropagation();
    if (!regBuildToggle.disabled) regBuildPanel.hidden = !regBuildPanel.hidden;
});
regBuildPanel.addEventListener('click', event => event.stopPropagation());
regBuildAll.addEventListener('click', () => selectRegressionBuilds(regBuildCatalog));
regBuildClear.addEventListener('click', () => selectRegressionBuilds([]));
regMetricToggle.addEventListener('click', event => {
    event.stopPropagation();
    if (!regMetricToggle.disabled) regMetricPanel.hidden = !regMetricPanel.hidden;
});
regMetricPanel.addEventListener('click', event => event.stopPropagation());
document.addEventListener('click', () => {
    regBuildPanel.hidden = true;
    regMetricPanel.hidden = true;
});
regMetricSpecified.addEventListener('click', () =>
    selectRegressionMetrics(specifiedRegressionMetricValues()));
regMetricAll.addEventListener('click', () =>
    selectRegressionMetrics(regMetricCatalog.map(option => option.value)));
regMetricClear.addEventListener('click', () => selectRegressionMetrics([]));
regReload.addEventListener('click', loadRegressionFilters);

async function runRegressionComparison() {
    if (![regDevice, regRam, regScenario].every(select => select.value)
            || !regSelectedMetrics.size || !regSelectedBuilds.size) {
        regressionStatus('Select one device, RAM configuration, scenario, one or more builds, and one or more metrics.', true);
        return;
    }
    const request = ++regRequest;
    const params = regressionParams();
    regMetricCatalog.forEach(option => {
        if (regSelectedMetrics.has(option.value)) params.append('metric', option.value);
    });
    regBuildCatalog.forEach(build => {
        if (regSelectedBuilds.has(build)) params.append('build', build);
    });
    params.set('direction', regDirection.value);
    params.set('exclude_under_8', regExcludeUnder8.checked ? '1' : '0');
    regApply.disabled = true;
    regressionStatus(`Comparing ${regSelectedBuilds.size} build(s) across ${regSelectedMetrics.size} metric(s)...`);
    try {
        const data = await regressionFetch(`/api/regression?${params}`);
        if (request === regRequest) renderRegression(data);
    } catch (error) {
        if (request === regRequest) regressionStatus(`Unable to compare builds: ${error.message}`, true);
    } finally {
        if (request === regRequest) updateRegressionApplyState();
    }
}

document.getElementById('regressionForm').addEventListener('submit', async event => {
    event.preventDefault();
    await runRegressionComparison();
});
