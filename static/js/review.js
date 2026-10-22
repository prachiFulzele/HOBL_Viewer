const reviewForm = document.getElementById('reviewForm');
const reviewStartDate = document.getElementById('reviewStartDate');
const reviewEndDate = document.getElementById('reviewEndDate');
const reviewBuild = document.getElementById('reviewBuild');
const reviewApply = document.getElementById('reviewApply');
const reviewResults = document.getElementById('reviewResults');

function localDateValue(date) {
    const offset = date.getTimezoneOffset() * 60000;
    return new Date(date.getTime() - offset).toISOString().slice(0, 10);
}

function setDefaultRange() {
    const end = new Date();
    const start = new Date(end);
    start.setDate(start.getDate() - 6);
    reviewStartDate.value = localDateValue(start);
    reviewEndDate.value = localDateValue(end);
    reviewStartDate.max = reviewEndDate.value;
    reviewEndDate.max = reviewEndDate.value;
}

function showReviewStatus(message, isError = false) {
    reviewResults.replaceChildren();
    const status = document.createElement('div');
    status.className = `status${isError ? ' error' : ''}`;
    status.textContent = message;
    reviewResults.appendChild(status);
}

function stat(value, label) {
    const item = document.createElement('div');
    item.className = 'review-stat';
    const valueElement = document.createElement('span');
    valueElement.className = 'review-stat-value';
    valueElement.textContent = Number(value).toLocaleString();
    const labelElement = document.createElement('span');
    labelElement.className = 'review-stat-label';
    labelElement.textContent = label;
    item.append(valueElement, labelElement);
    return item;
}

function appendReviewTable(container, rows) {
    const scroll = document.createElement('div');
    scroll.className = 'review-table-scroll';
    const table = document.createElement('table');
    table.className = 'review-table review-progress-table';
    const head = table.createTHead().insertRow();
    const labels = ['Scenario', 'Iterations', 'Target', 'Remaining', 'First upload', 'Last upload'];
    labels.forEach(label => {
        const cell = document.createElement('th');
        cell.textContent = label;
        cell.scope = 'col';
        head.appendChild(cell);
    });
    const body = table.createTBody();
    rows.forEach(row => {
        const tr = body.insertRow();
        const values = [
            row.scenario, Number(row.iterations).toLocaleString(),
            row.target, row.remaining, row.first_upload || '-', row.last_upload || '-',
        ];
        values.forEach(value => {
            const cell = tr.insertCell();
            cell.textContent = value;
        });
    });
    scroll.appendChild(table);
    container.appendChild(scroll);
}

function reviewSection(title) {
    const section = document.createElement('section');
    section.className = 'table-wrapper review-section';
    const heading = document.createElement('h3');
    heading.className = 'results-summary';
    heading.textContent = title;
    section.appendChild(heading);
    return section;
}

function renderSummary(data) {
    reviewResults.replaceChildren();
    reviewBuild.replaceChildren(new Option('All builds', ''));
    data.builds.forEach(build => reviewBuild.add(new Option(build, build)));
    reviewBuild.value = data.build;

    const stats = document.createElement('section');
    stats.className = 'review-stats';
    stats.append(
        stat(data.total_iterations, 'Distinct iterations'),
        stat(data.total_devices, 'Devices'),
        stat(data.total_scenarios, 'Scenarios'),
    );
    reviewResults.appendChild(stats);

    const wrapper = document.createElement('section');
    wrapper.className = 'table-wrapper';
    const summary = document.createElement('div');
    summary.className = 'results-summary';
    summary.textContent = `${data.start_date} through ${data.end_date} (inclusive) | ` +
        (data.build ? `Build ${data.build}` : 'All builds (iterations combined)');
    wrapper.appendChild(summary);

    if (!data.scenarios.length) {
        const empty = document.createElement('div');
        empty.className = 'review-empty';
        empty.textContent = 'No Kusto iterations were found in this date range.';
        wrapper.appendChild(empty);
        reviewResults.appendChild(wrapper);
        return;
    }

    reviewResults.appendChild(wrapper);
    const devices = new Map();
    data.scenarios.forEach(row => {
        if (!devices.has(row.device)) devices.set(row.device, []);
        devices.get(row.device).push(row);
    });
    devices.forEach((rows, device) => {
        const section = reviewSection(device);
        appendReviewTable(section, rows);
        reviewResults.appendChild(section);
    });

}

async function loadReviewSummary() {
    if (!reviewStartDate.value || !reviewEndDate.value) {
        showReviewStatus('Choose both a start date and an end date.', true);
        return;
    }
    if (reviewStartDate.value > reviewEndDate.value) {
        showReviewStatus('Start date must be on or before end date.', true);
        return;
    }

    reviewApply.disabled = true;
    reviewBuild.disabled = true;
    showReviewStatus('Loading summary from Kusto...');
    const params = new URLSearchParams({
        start_date: reviewStartDate.value,
        end_date: reviewEndDate.value,
        build: reviewBuild.value,
    });
    try {
        const response = await fetch(`/api/review-summary?${params}`);
        const data = await response.json();
        if (!response.ok) throw new Error(data.error || `Request failed (${response.status})`);
        renderSummary(data);
    } catch (error) {
        showReviewStatus(`Unable to load the summary: ${error.message}`, true);
    } finally {
        reviewApply.disabled = false;
        reviewBuild.disabled = false;
    }
}

reviewStartDate.addEventListener('change', () => {
    reviewEndDate.min = reviewStartDate.value;
    reviewBuild.replaceChildren(new Option('All builds', ''));
});
reviewEndDate.addEventListener('change', () => {
    reviewStartDate.max = reviewEndDate.value || localDateValue(new Date());
    reviewBuild.replaceChildren(new Option('All builds', ''));
});
reviewForm.addEventListener('submit', event => {
    event.preventDefault();
    loadReviewSummary();
});

setDefaultRange();
loadReviewSummary();