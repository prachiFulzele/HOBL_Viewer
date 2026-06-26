"""HOBL Dashboard – Flask app that serves an interactive metrics dashboard.

The data layer is pluggable: the active backend selects between a local-JSON
backend (``json_data``) and the Kusto backend (``kusto_data``). Both expose the
same ``get_filter_options`` / ``get_metrics`` / ``get_table_data`` surface and
record shape, so the routes and the frontend are identical regardless of source.

The active source can be switched at runtime from the dashboard UI (top-right
data-source control). In JSON mode the user uploads JSON files through the
browser; they are stored in ``config.UPLOAD_DIR`` and read by ``json_data``. The
chosen source is persisted to ``config.RUNTIME_STATE_FILE`` so it survives
restarts.
"""

import json as _json
import os

from flask import Flask, render_template, jsonify, request
from werkzeug.utils import secure_filename

import config
import json_data

# Kusto is optional: importing it may fail if the SDK isn't installed yet. The
# dashboard still works (in JSON mode) when it's unavailable.
try:
    import kusto_data
except Exception:  # pragma: no cover - environment dependent
    kusto_data = None

app = Flask(__name__)


# ── Runtime data-source state ────────────────────────────────────────────────

def _load_state() -> dict:
    """Load the persisted active source, falling back to the config default."""
    try:
        with open(config.RUNTIME_STATE_FILE, encoding="utf-8") as handle:
            data = _json.load(handle)
        if data.get("source") in ("json", "kusto"):
            return {"source": data["source"]}
    except (OSError, ValueError):
        pass
    return {"source": config.DATA_SOURCE}


_state = _load_state()


def _save_state() -> None:
    try:
        with open(config.RUNTIME_STATE_FILE, "w", encoding="utf-8") as handle:
            _json.dump(_state, handle)
    except OSError:
        pass


def _kusto_available() -> bool:
    return kusto_data is not None


def get_backend():
    """Return the module backing the currently-selected data source."""
    if _state["source"] == "kusto" and _kusto_available():
        return kusto_data
    return json_data


def _uploaded_json_count() -> int:
    try:
        return sum(1 for n in os.listdir(config.UPLOAD_DIR) if n.lower().endswith(".json"))
    except OSError:
        return 0


# ── Routes ───────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/datasource", methods=["GET", "POST"])
def api_datasource():
    """Get or set the active data source.

    POST body: {"source": "json" | "kusto"}. A request to switch to Kusto when
    it isn't available is rejected.
    """
    if request.method == "POST":
        data = request.get_json(silent=True) or {}
        src = data.get("source")
        if src not in ("json", "kusto"):
            return jsonify({"error": "source must be 'json' or 'kusto'"}), 400
        if src == "kusto" and not _kusto_available():
            return jsonify({"error": "Kusto backend is not available."}), 400
        _state["source"] = src
        _save_state()
    return jsonify({
        "source": _state["source"],
        "kusto_available": _kusto_available(),
        "json_count": _uploaded_json_count(),
    })


@app.route("/api/datasource/upload", methods=["POST"])
def api_datasource_upload():
    """Accept one or more uploaded JSON files for JSON mode."""
    os.makedirs(config.UPLOAD_DIR, exist_ok=True)
    saved, skipped = 0, 0
    for f in request.files.getlist("files"):
        name = f.filename or ""
        if not name.lower().endswith(".json"):
            skipped += 1
            continue
        safe = secure_filename(name) or f"upload_{saved}.json"
        if not safe.lower().endswith(".json"):
            safe += ".json"
        try:
            f.save(os.path.join(config.UPLOAD_DIR, safe))
            saved += 1
        except OSError:
            skipped += 1
    return jsonify({"saved": saved, "skipped": skipped, "total": _uploaded_json_count()})


@app.route("/api/datasource/clear", methods=["POST"])
def api_datasource_clear():
    """Remove all uploaded JSON files."""
    try:
        for n in os.listdir(config.UPLOAD_DIR):
            if n.lower().endswith(".json"):
                os.remove(os.path.join(config.UPLOAD_DIR, n))
    except OSError:
        pass
    return jsonify({"total": _uploaded_json_count()})


@app.route("/ai")
def ai_page():
    """Optional AI Analysis page (natural-language → table/insights/chart)."""
    return render_template("ai.html")


@app.route("/api/ai/analyze", methods=["POST"])
def api_ai_analyze():
    """Run a natural-language analysis against the active data source.

    Body: ``{"question": str, "context": {"spec": <previous query plan>}?}``.
    When ``context.spec`` is present the question refines that existing view.
    """
    import ai_analysis

    payload = request.get_json(silent=True) or {}
    question = payload.get("question", "")
    context = payload.get("context")
    clarification = payload.get("clarification")
    try:
        return jsonify(ai_analysis.analyze(question, context, clarification))
    except Exception as exc:  # surfaced to the UI as a friendly error
        return jsonify({"error": str(exc)}), 500


@app.route("/api/filters")
def api_filters():
    """Return distinct values for each independent, optional filter."""
    try:
        return jsonify(get_backend().get_filter_options())
    except Exception as exc:  # surfaced to the UI as a friendly error
        return jsonify({"error": str(exc)}), 500


def _parse_filters():
    """Parse the shared optional filter query params used by metrics + table."""
    def _int(name):
        raw = request.args.get(name, "")
        try:
            return int(raw) if raw.strip() else None
        except ValueError:
            return None

    def _list(name):
        """Collect a multi-valued filter: repeated params and/or comma-separated."""
        out = []
        for raw in request.args.getlist(name):
            out.extend(p.strip() for p in raw.split(",") if p.strip())
        return out

    return {
        "device": _list("device"),
        "ram": _list("ram"),
        "scenario": _list("scenario"),
        "start_date": request.args.get("start_date", ""),
        "end_date": request.args.get("end_date", ""),
        "last_n": _int("last_n"),
        "start_iter": _int("start_iter"),
        "end_iter": _int("end_iter"),
    }


@app.route("/api/metrics")
def api_metrics():
    """Return metrics matching the selected filters. Any omitted filter = all."""
    f = _parse_filters()
    try:
        return jsonify(
            get_backend().get_metrics(
                f["device"], f["ram"], f["scenario"], f["start_date"],
                f["end_date"], f["last_n"], f["start_iter"], f["end_iter"],
            )
        )
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/table")
def api_table():
    """Return per-iteration columns for the transposed Excel-style table view."""
    f = _parse_filters()
    try:
        return jsonify(
            get_backend().get_table_data(
                f["device"], f["ram"], f["scenario"], f["start_date"],
                f["end_date"], f["last_n"], f["start_iter"], f["end_iter"],
            )
        )
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


# ── Entry point ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print(f"Starting HOBL Dashboard on http://{config.HOST}:{config.PORT}")
    print(f"Active data source: {_state['source']}")
    if _state["source"] == "kusto":
        print("A browser window will open for Azure AD sign-in...")
    app.run(host=config.HOST, port=config.PORT, debug=config.DEBUG)
