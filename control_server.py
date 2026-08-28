"""
Control API for the merged run_bots.py process: per-bot on/off toggles.

Runs as a daemon thread inside run_bots.py (like franklin/control.py does
for the web dashboard, which stays on its own port 8766). The a-bot panel
proxies to this over localhost.

  GET  /bots            -> {"pinger": true, ...}
  POST /bots/<name>/on  -> {"<name>": true}
  POST /bots/<name>/off -> {"<name>": false}
  GET  /accountability/export -> full accountability data (self-describing
                                 JSON — see its "_api" key for the schema)
"""
import os

from flask import Flask, jsonify

import bot_state

app = Flask(__name__)


@app.route("/bots")
def bots():
    return jsonify(bot_state.all_states())


@app.route("/bots/<name>/on", methods=["POST"])
def bot_on(name: str):
    try:
        bot_state.set_enabled(name, True)
    except KeyError:
        return jsonify({"error": f"unknown bot {name}"}), 404
    return jsonify({name: True})


@app.route("/bots/<name>/off", methods=["POST"])
def bot_off(name: str):
    try:
        bot_state.set_enabled(name, False)
    except KeyError:
        return jsonify({"error": f"unknown bot {name}"}), 404
    return jsonify({name: False})


@app.route("/accountability/export")
def accountability_export():
    """Machine-readable accountability data for Claude and other bots.
    The payload is self-describing — see its `_api` key."""
    import accountability_bot
    return jsonify(accountability_bot.build_export())


def run() -> None:
    port = int(os.environ.get("RUNBOTS_CONTROL_PORT", "8767"))
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)
