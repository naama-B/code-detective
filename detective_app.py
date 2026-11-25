"""Code Detective - the popup window.

Runs in the background (started by the PowerShell profile). It watches the PowerShell logs,
and when a command fails it opens a window on the right side of the screen with an
explanation in Hebrew and two buttons: fix it myself (hint) / give me the solution.

Closing the window only hides it. The next error opens it again.
To stop the agent completely, use the "כיבוי" button in the window.
"""
import socket
import sys

# ---------- Only one copy may run ----------
# Every new PowerShell window tries to start the app. The first copy takes this port;
# any later copy cannot take it, so it closes itself at once (before loading anything heavy).
_single_instance = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
try:
    _single_instance.bind(("127.0.0.1", 47831))
except OSError:
    sys.exit(0)

import ctypes
import json
import logging
import os
import subprocess
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path

import webview
from langgraph.types import Command

from graph import VERSIONS, build_graph
from watcher import LogWatcher

HERE = Path(__file__).parent
APP_LOG = Path.home() / "detective" / "app.log"   # pythonw has no console, so problems are written here
APP_LOG.parent.mkdir(parents=True, exist_ok=True)
logging.basicConfig(filename=APP_LOG, level=logging.INFO, encoding="utf-8",
                    format="%(asctime)s %(levelname)s %(message)s")

# The newest version (v3), with explanations in Hebrew
SETTINGS = {**VERSIONS["v3"],
            "language": "Hebrew. Simple and clear. Address the student in the feminine form"}
WINDOW_WIDTH = 470


class Detective:
    """The brain of the app: watches the logs, runs the graph, and talks to the window."""

    def __init__(self):
        self.graph = build_graph()
        self.watcher = LogWatcher()          # starts from "now": old errors are ignored
        self.window = None
        self.configs = {}                    # card id -> the graph config (thread_id) of that error

    # ---------- watching (runs in a background thread) ----------
    def watch_forever(self):
        logging.info("Code Detective started")
        while True:
            try:
                for item in self.watcher.poll():
                    # each command is analyzed in its own thread, so the watching never stops
                    threading.Thread(target=self.analyze, args=(item,), daemon=True).start()
            except Exception:
                logging.exception("watcher failed")
            time.sleep(0.25)

    def analyze(self, item: dict):
        card_id = str(uuid.uuid4())
        config = {"configurable": {"thread_id": card_id, **SETTINGS}, "tags": ["app"]}
        shown = False
        try:
            # stream = run the graph step by step, so we know about the error BEFORE the model answers
            for update in self.graph.stream(item, config):
                detected = update.get("detect_error") or {}
                if detected.get("has_error") and not shown:
                    # pop up at once with "analyzing...", and fill in the explanation when it is ready
                    self.send("addPendingCard", {"id": card_id, "time": now(),
                                                 "command": item["command"], "cwd": item["cwd"]})
                    shown = True
        except Exception as error:
            logging.exception("analysis failed for %s", item["command"])
            self.send("addFailedCard", {"id": card_id, "time": now(), "command": item["command"],
                                        "reason": str(error)[:200]}, bring_to_front=not shown)
            return

        snapshot = self.graph.get_state(config)
        if not snapshot.next:                # the graph ended without stopping: there was no error
            return

        # The graph is paused at choose_mode (the interrupt): fill the card and wait for a button
        state = snapshot.values
        self.configs[card_id] = config
        self.send("addCard", {
            "id": card_id, "time": now(), "command": item["command"], "cwd": item["cwd"],
            "error_type": state.get("error_type", ""),
            "file": os.path.basename(state.get("file_path", "")),
            "line": state.get("line", 0),
            "explanation": state.get("explanation", ""),
        }, bring_to_front=not shown)

    # ---------- the buttons (called from the window) ----------
    def choose(self, card_id: str, mode: str) -> dict:
        """Continues the paused graph with the student's choice: "self" (hint) or "solution"."""
        config = self.configs.get(card_id)
        if not config:
            return {"ok": False}
        try:
            self.graph.invoke(Command(resume=mode), config)
        except Exception:
            logging.exception("resume failed")
            return {"ok": False}

        state = self.graph.get_state(config).values
        if mode == "self":
            return {"ok": True, "mode": "self", "hint": state.get("hint", "")}
        return {"ok": True, "mode": "solution",
                "file": os.path.basename(state.get("fix_file") or ""),
                "line": state.get("fix_line", 0),
                "current_code": state.get("current_code", ""),
                "new_code": state.get("new_code", ""),
                "fix": state.get("fix", ""),
                "why_it_works": state.get("why_it_works", "")}

    # ---------- the window ----------
    def send(self, js_function: str, data: dict, bring_to_front: bool = True):
        """Calls a JavaScript function in the window with data, and (by default) shows the window."""
        if not self.window:
            return
        self.window.evaluate_js(f"{js_function}({json.dumps(data, ensure_ascii=False)})")
        if not bring_to_front:
            return
        self.window.show()
        self.window.restore()
        self.window.on_top = True            # jump in front of PowerShell ...
        time.sleep(0.3)
        self.window.on_top = False           # ... and then behave like a normal window


class Api:
    """The functions the buttons in the window can call (window.pywebview.api.<name>)."""

    def __init__(self, detective: Detective):
        self._detective = detective           # "_" = hidden from the window

    def choose(self, card_id, mode):
        return self._detective.choose(card_id, mode)

    def copy(self, text):
        # Windows' clip command; UTF-16 so Hebrew and symbols are copied correctly
        subprocess.run(["clip"], input=text.encode("utf-16"), check=False)

    def quit(self):
        logging.info("stopped by the user")
        os._exit(0)


def now() -> str:
    return datetime.now().strftime("%H:%M")


def main():
    # Our own taskbar identity, so Windows shows OUR icon and not Python's
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("CodeDetective.App")
    except Exception:
        pass

    detective = Detective()

    # Place the window on the right side of the main screen
    try:
        screen = webview.screens[0]
        x, height = screen.width - WINDOW_WIDTH - 10, screen.height - 90
    except Exception:
        x, height = None, 800

    window = webview.create_window(
        "Code Detective", html=(HERE / "ui.html").read_text(encoding="utf-8"),
        js_api=Api(detective), width=WINDOW_WIDTH, height=height, x=x, y=10,
        hidden=True, text_select=True, background_color="#f6f5f2")
    detective.window = window

    def on_closing():
        window.hide()                         # closing = hiding. The next error opens it again.
        return False                          # False = do not really close

    window.events.closing += on_closing
    # The window must run on the main thread. icon.ico = the icon in the window and the taskbar.
    webview.start(detective.watch_forever, icon=str(HERE / "icon.ico"))


if __name__ == "__main__":
    main()
