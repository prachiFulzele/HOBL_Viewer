# HOBL Dashboard — Design Document

## 1. Overview

The **HOBL Dashboard** is a web-based tool that visualizes performance and power metrics collected from HOBL (Hardware-Optimized Benchmark Lab) scenario runs. It queries the **Fungates Kusto database** and presents the data in an interactive, filterable HTML table.

The dashboard allows engineers to:
- Select a **device** and its **RAM configuration**
- Choose a **scenario** (e.g., YouTube, Teams, etc.)
- Pick a **date** when the scenario was run
- View all **metrics iteration-wise** in a structured table

---

## 2. End-to-End Data Flow

The dashboard is the final consumer in a pipeline that starts with HOBL running a scenario:

```
┌─────────────┐     ┌───────────────────┐     ┌──────────────────┐     ┌─────────────────┐
│  HOBL runs   │     │  Results extracted │     │  Fungates Azure  │     │  HOBL Dashboard  │
│  a scenario  │────▶│  to JSON + ETL    │────▶│  Function ingests│────▶│  queries Kusto   │
│  (PASS)      │     │  & uploaded via   │     │  into Kusto DB   │     │  & displays data │
│              │     │  CSEUploader.exe  │     │  (Hobl_RawMetrics│     │                  │
└─────────────┘     └───────────────────┘     └──────────────────┘     └─────────────────┘
```

### Step-by-step:

1. **HOBL** (`C:\HOBL`) runs a scenario on a device. If the result is **PASS** and `fungates_upload_enabled=1`, it automatically:
   - Extracts results into `hobl_result.json` (via `utilities\extractor\json_builder.py`)
   - Uploads `hobl_result.json` + `trace.etl` to the Fungates portal (via `Upload-ToFungates.ps1` → `CSEUploader.exe`)

2. **Fungates Azure Function** (`fungates-hobl`) picks up the upload:
   - Downloads the JSON and ETL blobs
   - Runs `HoblMetricExtractor.exe` to parse `hobl_result.json` into CSV metrics
   - Writes the metrics to the **Kusto** table `Hobl_RawMetrics` in the `FungatesDataStore` database

3. **HOBL Dashboard** (`C:\hobl-dashboard`) queries the Kusto table and renders the data in the browser

---

## 3. Architecture

```
┌──────────────────────────────────────────────────┐
│                  User's Browser                   │
│                                                   │
│   ┌───────────────────────────────────────────┐   │
│   │         HOBL Dashboard (HTML/JS)          │   │
│   │                                           │   │
│   │  ┌─────────────┐ ┌──────┐ ┌──────┐       │   │
│   │  │Device & RAM │ │Scenar│ │ Date │       │   │
│   │  │  dropdown   │ │  io  │ │      │       │   │
│   │  └──────┬──────┘ └──┬───┘ └──┬───┘       │   │
│   │         │ cascading │        │            │   │
│   │         ▼           ▼        ▼            │   │
│   │  ┌─────────────────────────────────┐      │   │
│   │  │  Metrics Table (grouped by      │      │   │
│   │  │  iteration)                     │      │   │
│   │  └─────────────────────────────────┘      │   │
│   └────────────────────┬──────────────────────┘   │
└────────────────────────┼──────────────────────────┘
                         │ HTTP (localhost:5000)
                         ▼
              ┌─────────────────────┐
              │  Flask Backend      │
              │  (app.py)           │
              │                     │
              │  /api/tiers         │
              │  /api/scenarios     │
              │  /api/dates         │
              │  /api/metrics       │
              └─────────┬───────────┘
                        │ KQL queries (azure-kusto-data SDK)
                        │ Authenticated via Azure AD
                        ▼
              ┌─────────────────────┐
              │  Azure Data Explorer│
              │  (Kusto)            │
              │                     │
              │  Cluster: fungateprd│
              │  DB: FungatesData-  │
              │      Store          │
              │  Table: Hobl_Raw-   │
              │         Metrics     │
              └─────────────────────┘
```

### Tech Stack

| Component      | Technology                                      |
|----------------|------------------------------------------------|
| Backend        | Python 3 + Flask                                |
| Kusto SDK      | `azure-kusto-data` (KQL queries)                |
| Authentication | `azure-identity` (InteractiveBrowserCredential) |
| Frontend       | HTML + CSS + vanilla JavaScript                  |
| Data Source     | Azure Data Explorer (Kusto)                     |

---

## 4. Authentication

The dashboard uses **Azure AD interactive browser login** to authenticate the user.

### How it works:

1. When the Flask app starts, it initializes an `InteractiveBrowserCredential` from the `azure-identity` SDK.
2. On the first Kusto query, a **browser tab opens automatically** showing the Microsoft Azure login page:

   > **Screenshot — Login Page:**
   > The user sees the standard Microsoft "Pick an account" screen. They select their Microsoft corporate account (e.g., `t-anaagarwal@microsoft.com`) to authenticate.

3. After successful login, the browser shows **"Authentication complete. You can close this window."**:

   > **Screenshot — Authentication Complete:**
   > A confirmation page appears in the browser tab. The user can close this tab and return to the dashboard.

4. The token is cached for the session — subsequent queries do **not** require re-login.

### Access Control:

- **Only users with Azure AD accounts that have RBAC access to the Kusto cluster** (`fungateprd.centralus`) can query data.
- The dashboard runs on `localhost` (127.0.0.1), so only the person running it on their machine can access the web UI.
- No data is exposed externally.

---

## 5. Dashboard UI

> **Screenshot — Dashboard Page:**
> The dashboard shows a blue header bar titled "HOBL Dashboard", a filter panel with three dropdowns (Device & RAM Config, Scenario, Date), and a status message area below.

### 5.1 Filters (Cascading Dropdowns)

The dashboard has three cascading filter dropdowns that load data progressively:

| Filter             | Source Column   | Description                                                        |
|--------------------|----------------|--------------------------------------------------------------------|
| **Device & RAM Config** | `TierName`     | Format: `<DeviceName>_<MemorySizeGB>`. Displayed as `DeviceName (XX GB)` |
| **Scenario**       | `TestName`      | Extracted from TestName by removing the `_NNN` iteration suffix     |
| **Date**           | `RunDate`       | The date when the HOBL scenario was run, formatted as `YYYY-MM-DD` |

**Cascading behavior:**
1. **Device & RAM Config** loads on page open → lists all available devices
2. **Scenario** loads when a device is selected → shows scenarios run on that device
3. **Date** loads when a scenario is selected → shows dates that scenario was run
4. **Metrics table** loads when a date is selected → shows all metrics for that run

### 5.2 Metrics Table

Once all three filters are selected, the metrics are displayed in a table with the following columns:

| Column         | Source Column | Description                                                     |
|----------------|---------------|-----------------------------------------------------------------|
| **Iteration**  | `Iteration`   | The iteration number (1, 2, 3...) — grouped with a section header |
| **Metric Name**| `Name`        | The metric identifier (e.g., `pm_emi_cpu_cluster_0`, `system_power`) |
| **Value**      | `Value`       | The numeric metric value (e.g., `0.177`, `4.68`)                 |
| **Unit**       | `Unit`        | Unit of measurement (e.g., `W` for Watts, `ms` for milliseconds) |
| **Type**       | `MetricType`  | Color-coded badge: `PowerMetrics` (green), `PowerCalculation` (orange), `PerfMetrics` (blue) |

**Grouping:** Metrics are grouped by iteration. A blue header row separates each iteration (e.g., "Iteration 1", "Iteration 2"). Within each iteration, metrics are sorted by type and then by name.

---

## 6. Kusto Table Schema

The dashboard reads from the `Hobl_RawMetrics` table in the `FungatesDataStore` database. Key columns used:

| Column          | Type     | Used For                                    |
|-----------------|----------|---------------------------------------------|
| `Name`          | string   | Metric name displayed in the table          |
| `MetricType`    | string   | Metric category (PowerMetrics, etc.)        |
| `StudyType`     | string   | Run type (e.g., "Power")                    |
| `Value`         | real     | Metric value displayed in the table         |
| `Unit`          | string   | Unit of measurement                         |
| `TierName`      | string   | `<DeviceName>_<RAMSizeGB>` — used for device filter |
| `TestName`      | string   | `<scenario>_<iteration>` — used for scenario filter |
| `RunDate`       | datetime | Date of the run — used for date filter      |
| `Iteration`     | int      | Iteration number — used for table grouping  |
| `TestResultId`  | string   | Unique ID for the test result               |

### How TierName maps to Device + RAM:

The `TierName` is generated during HOBL's upload process (`New-MetadataFile.ps1`) as:
```
TierDefinitionKey = "<DeviceName>_<MemorySizeGB>"
```
For example: `ROM-MS-IDCLAB-2_16` → Device: `ROM-MS-IDCLAB-2`, RAM: `16 GB`

### How TestName maps to Scenario + Iteration:

The `TestName` is the ETL filename without extension (e.g., `youtube_001`). The dashboard extracts:
- **Scenario**: everything before the last `_NNN` → `youtube`
- **Iteration**: the numeric suffix → `1`

---

## 7. API Endpoints

The Flask backend exposes four REST endpoints:

| Endpoint           | Method | Parameters                      | Returns                        |
|--------------------|--------|---------------------------------|--------------------------------|
| `/api/tiers`       | GET    | —                               | List of distinct `TierName` values |
| `/api/scenarios`   | GET    | `tier` (TierName)               | List of scenario names for that tier |
| `/api/dates`       | GET    | `tier`, `scenario`              | List of run dates (YYYY-MM-DD) |
| `/api/metrics`     | GET    | `tier`, `scenario`, `date`      | List of metric objects with Iteration, MetricName, Value, Unit, MetricType |

All endpoints return JSON. Input parameters are sanitized to prevent KQL injection.

---

## 8. Project Structure

```
C:\hobl-dashboard\
├── venv\                      # Python virtual environment
├── templates\
│   └── index.html             # Dashboard UI (HTML + CSS + JavaScript)
├── app.py                     # Flask backend with Kusto query logic
├── config.py                  # Configuration (Kusto cluster, DB, table)
├── requirements.txt           # Python dependencies
└── DESIGN_DOCUMENT.md         # This document
```

---

## 9. How to Run

### Prerequisites
- Python 3.10+
- VPN connection (to reach the Kusto cluster `fungateprd.centralus`)
- Azure AD account with access to the `FungatesDataStore` Kusto database

### Steps

```powershell
cd C:\hobl-dashboard
.\venv\Scripts\Activate.ps1
python app.py
```

1. The Flask server starts on `http://127.0.0.1:5000`
2. Open that URL in your browser
3. A new tab opens for **Azure AD login** — pick your Microsoft account
4. After "Authentication complete", return to the dashboard tab
5. Use the cascading filters to explore metrics

### First-time Setup (if venv doesn't exist)

```powershell
cd C:\hobl-dashboard
python -m venv venv
.\venv\Scripts\Activate.ps1
pip install -r requirements.txt
python app.py
```

---

## 10. Dependencies

| Package            | Version | Purpose                                  |
|--------------------|---------|------------------------------------------|
| `flask`            | 3.1.1   | Web framework for the dashboard server   |
| `azure-kusto-data` | 4.6.1   | Kusto / Azure Data Explorer SDK          |
| `azure-identity`   | 1.21.0  | Azure AD authentication (browser login)  |

---

## 11. Future Enhancements

- **CSV Export**: Add a button to download the displayed metrics as a CSV file
- **Trend Charts**: Plot metric values across dates for the same device + scenario
- **Comparison View**: Compare metrics between two different devices or dates side by side
- **Team Deployment**: Host on Azure App Service with Azure AD web app authentication for team-wide access
- **Auto-Refresh**: Periodically poll for new data after scenario uploads
