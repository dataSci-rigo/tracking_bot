import logging
import os
import threading
import time
from datetime import date, timedelta

from flask import Flask, flash, redirect, render_template, request, url_for
from werkzeug.serving import make_server

import store

logger = logging.getLogger(__name__)

app = Flask(__name__, template_folder="templates", static_folder="static")
app.secret_key = os.environ.get("FLASK_SECRET", "franklin-secret-change-me")

_server = None
_thread = None
_last_activity = 0.0
_stop_event = threading.Event()
_lock = threading.Lock()
_IDLE_TIMEOUT = 1800  # 30 minutes


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------

def is_running() -> bool:
    with _lock:
        return _thread is not None and _thread.is_alive()


def start_server() -> None:
    global _server, _thread, _last_activity
    with _lock:
        if _thread is not None and _thread.is_alive():
            _last_activity = time.time()
            return
        port = int(os.environ.get("WEB_PORT", "8765"))
        _stop_event.clear()
        _last_activity = time.time()
        _server = make_server("0.0.0.0", port, app)
        _thread = threading.Thread(target=_run_server, daemon=True)
        _thread.start()
        watcher = threading.Thread(target=_idle_watcher, daemon=True)
        watcher.start()
        logger.info("Web server started on port %d", port)


def stop_server() -> None:
    global _server, _thread
    with _lock:
        if _server is None:
            return
        _stop_event.set()
        _server.shutdown()
        _server = None
    if _thread is not None:
        _thread.join(timeout=5)
        _thread = None
    logger.info("Web server stopped")


def _run_server():
    try:
        _server.serve_forever()
    except Exception:
        pass


def _idle_watcher():
    while not _stop_event.is_set():
        time.sleep(60)
        if _stop_event.is_set():
            break
        idle = time.time() - _last_activity
        if idle >= _IDLE_TIMEOUT:
            logger.info("Idle timeout — shutting down web server")
            stop_server()
            break


# ---------------------------------------------------------------------------
# Request hooks
# ---------------------------------------------------------------------------

@app.before_request
def _touch():
    global _last_activity
    _last_activity = time.time()


@app.after_request
def _touch_after(response):
    global _last_activity
    _last_activity = time.time()
    return response


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    config = store.load_config()
    virtues = config["virtues"]
    virtue = store.current_focus_virtue()

    today = date.today()
    monday = today - timedelta(days=today.weekday())
    week_days = []
    for i in range(7):
        d = monday + timedelta(days=i)
        week_days.append({
            "date": d.isoformat(),
            "label": d.strftime("%a %-m/%-d"),
            "is_today": d == today,
        })

    marks = store.get_marks(monday.isoformat(), (monday + timedelta(days=6)).isoformat())

    notes_raw = store.get_notes()
    notes_raw.sort(key=lambda n: n["ts"], reverse=True)
    recent_notes = []
    for n in notes_raw[:5]:
        v_name = next((v["name"] for v in virtues if v["id"] == n.get("virtue")), "General")
        recent_notes.append({**n, "virtue_name": v_name, "date": n["ts"][:10]})

    return render_template(
        "form.html",
        week_days=week_days,
        virtues=virtues,
        marks=marks,
        focus_id=virtue["id"],
        recent_notes=recent_notes,
        today=today.isoformat(),
    )


@app.route("/save", methods=["POST"])
def save():
    config = store.load_config()
    virtues = config["virtues"]
    today_str = date.today().isoformat()

    for v in virtues:
        key = f"mark_{v['id']}"
        raw = request.form.get(key, "0").strip()
        try:
            count = int(raw)
        except ValueError:
            count = 0
        if count < 0:
            count = 0
        store.add_mark(today_str, v["id"], count)

    note_text = request.form.get("note_text", "").strip()
    if note_text:
        note_virtue = request.form.get("note_virtue") or None
        if note_virtue == "general":
            note_virtue = None
        store.add_note(note_virtue, note_text)

    from datetime import datetime
    import pytz
    la = pytz.timezone("America/Los_Angeles")
    now = datetime.now(la).strftime("%H:%M")
    flash(f"Saved at {now}")
    return redirect(url_for("index"))


@app.route("/shutdown", methods=["POST"])
def shutdown():
    threading.Timer(0.5, stop_server).start()
    return (
        "<html><body style='font-family:sans-serif;padding:2rem'>"
        "<h2>Server shutting down.</h2>"
        "<p>You can close this tab.</p>"
        "</body></html>"
    )
