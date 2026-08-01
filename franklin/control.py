"""
Tiny always-on control server for Franklin's on-demand web dashboard
(web.py). web.py's own Flask app only listens once start_server() has been
called, so nothing can be reached to *ask* it to start — this small sibling
runs for the lifetime of the process instead, exposing just enough to let
the control panel start/stop/check the dashboard directly (no systemd
service exists for the dashboard itself; it's a thread inside app-todo).
"""
import os

from flask import Flask, jsonify

import web as web_mod

app = Flask(__name__)


@app.route("/status")
def status():
    return jsonify(running=web_mod.is_running())


@app.route("/start", methods=["POST"])
def start():
    web_mod.start_server()
    return jsonify(running=True)


@app.route("/stop", methods=["POST"])
def stop():
    web_mod.stop_server()
    return jsonify(running=False)


def run() -> None:
    port = int(os.environ.get("FRANKLIN_CONTROL_PORT", "8766"))
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)
