"""Watches the PowerShell transcript logs and returns every finished command with its output.

Every PowerShell window writes to its own file in  %USERPROFILE%\\detective\\logs.
A command line in the log looks like:   PS C:\\Users\\user1\\project> python app.py
(or with a venv:                        (.venv) PS C:\\Users\\user1\\project> python app.py)
Everything after it, until the next command line, is that command's output.

    python watcher.py --test     # prints every finished command (to check it works)
"""
import os
import re
import sys
import time
from pathlib import Path

LOG_DIR = Path.home() / "detective" / "logs"

# A command line: optional "(.venv) ", then "PS <folder>> <command>"
PROMPT = re.compile(r"^(?:\(.+?\)\s*)?PS (?P<cwd>[A-Za-z]:\\.*?)> ?(?P<command>.*)$")
STARS = "**********************"     # the header/footer lines PowerShell adds to every log

# PowerShell writes the NEXT command line only when you run the next command.
# So we also treat a command as finished when its output has been quiet for this long.
QUIET_SECONDS = 1.0


class LogWatcher:
    def __init__(self, log_dir: Path = LOG_DIR, skip_existing: bool = True, ignore_pid: int | None = None):
        self.log_dir = log_dir
        self.ignore_pid = ignore_pid      # a PowerShell window whose log we must not read (our own)
        self.files = {}                   # file path -> what we know about that file
        if skip_existing:                 # do not analyze old history, only what happens from now on
            for path in self._log_files():
                self.files[path] = self._new_file_state(start_at=self._read(path).__len__())

    # ---------- public ----------
    def poll(self) -> list[dict]:
        """Reads what was added to the logs since last time. Returns the finished commands."""
        finished = []
        for path in self._log_files():
            state = self.files.setdefault(path, self._new_file_state(start_at=0))
            text = self._read(path)
            new_text = text[state["offset"]:]
            state["offset"] = len(text)
            if new_text:
                finished += self._feed(state, new_text)
            finished += self._check_quiet(state)
        return finished

    # ---------- helpers ----------
    def _log_files(self) -> list[Path]:
        if not self.log_dir.exists():
            return []
        files = sorted(self.log_dir.glob("session-*.log"))
        if self.ignore_pid:
            files = [f for f in files if not f.stem.endswith(f"-{self.ignore_pid}")]
        return files

    @staticmethod
    def _read(path: Path) -> str:
        try:
            return path.read_text(encoding="utf-8-sig", errors="replace")
        except OSError:                   # the file is being written right now: try next time
            return ""

    @staticmethod
    def _new_file_state(start_at: int) -> dict:
        return {"offset": start_at, "partial": "", "in_header": False,
                "current": None, "last_change": time.time()}

    def _feed(self, state: dict, new_text: str) -> list[dict]:
        finished = []
        text = state["partial"] + new_text
        lines = text.split("\n")
        state["partial"] = lines.pop()    # the last piece may be a half-written line
        for raw in lines:
            line = raw.rstrip("\r")
            if line.strip() == STARS:     # skip PowerShell's header and footer blocks
                state["in_header"] = not state["in_header"]
                continue
            if state["in_header"]:
                continue
            match = PROMPT.match(line)
            if match:
                if state["current"]:
                    finished.append(self._finish(state))
                state["current"] = {"command": match["command"].strip(), "cwd": match["cwd"],
                                    "lines": []}
            elif state["current"] is not None:
                state["current"]["lines"].append(line)
            state["last_change"] = time.time()
        return [f for f in finished if f]

    def _check_quiet(self, state: dict) -> list[dict]:
        current = state["current"]
        quiet = time.time() - state["last_change"] >= QUIET_SECONDS
        if current and current["lines"] and quiet:
            done = self._finish(state)
            # more output may still come later for the same command: keep collecting it
            state["current"] = {"command": done["command"], "cwd": done["cwd"], "lines": []} if done else None
            return [done] if done else []
        return []

    @staticmethod
    def _finish(state: dict) -> dict | None:
        current = state["current"]
        state["current"] = None
        output = "\n".join(current["lines"]).strip()
        if not current["command"] or not output:
            return None                   # an empty line, or a command with no output (like cd)
        return {"command": current["command"], "output": output, "cwd": current["cwd"]}


if __name__ == "__main__":
    # Test mode: print every finished command. Our own window is ignored, so we do not read ourselves.
    watcher = LogWatcher(ignore_pid=os.getppid())
    print(f"Watching {LOG_DIR} ... run commands in ANOTHER PowerShell window. Ctrl+C to stop.")
    while True:
        for item in watcher.poll():
            print("=" * 60)
            print(f"COMMAND: {item['command']}")
            print(f"FOLDER:  {item['cwd']}")
            print(item["output"][-400:])
        time.sleep(0.5)
