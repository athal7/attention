#!/usr/bin/env python3
"""Exercise the real curses CLI with embedded-terminal arrow encodings."""
import errno
import fcntl
import json
import os
from pathlib import Path
import pty
import select
import struct
import subprocess
import sys
import tempfile
import termios
import time
import unittest

ROOT = Path(__file__).resolve().parent


class TerminalInputTests(unittest.TestCase):
    def launch(self, grouped):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        config_dir = Path(tmp.name) / "attention"
        config_dir.mkdir()
        records = [
            {"id": "alpha-1", "title": "Alpha one", "group": "Alpha", "weight": 4},
            {"id": "alpha-2", "title": "Alpha two", "group": "Alpha", "weight": 3},
            {"id": "beta-1", "title": "Beta one", "group": "Beta", "weight": 2},
            {"id": "beta-2", "title": "Beta two", "group": "Beta", "weight": 1},
        ]
        config = {
            "plugins": ["generic"],
            "generic": {"fixture": {
                "command": [sys.executable, "-c", "print(" + repr(json.dumps(records)) + ")"],
                "title": "{title}", "id": "{id}", "context": "{group}",
                "kind": "{group}", "weight": "{weight}",
                "actions": [{"key": "o", "label": "select", "command": [
                    sys.executable, "-c", "print('PICKED {id}', flush=True)",
                ]}],
            }},
        }
        if grouped:
            config["dashboard"] = {"groups": [
                {"name": "Alpha", "match": {"kinds": ["Alpha"]}},
                {"name": "Beta", "fallback": True},
            ]}
        else:
            config["dashboard"] = {"groups": []}
        (config_dir / "config.json").write_text(json.dumps(config))
        master, slave = pty.openpty()
        self.addCleanup(os.close, master)
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 100, 0, 0))
        env = dict(os.environ, TERM="xterm-256color", XDG_CONFIG_HOME=tmp.name,
                   XDG_STATE_HOME=tmp.name, XDG_CACHE_HOME=tmp.name)
        env.pop("ESCDELAY", None)
        try:
            self.process = subprocess.Popen(
                [sys.executable, str(ROOT / "attention")],
                stdin=slave, stdout=slave, stderr=slave, env=env,
            )
        finally:
            os.close(slave)
        self.master = master
        self.transcript = b""
        self.addCleanup(self.stop_process)
        self.wait_for(b"Alpha (2)" if grouped else b"Alpha two")

    def stop_process(self):
        if self.process.poll() is None:
            self.process.kill()
        self.process.wait(timeout=5)

    def read_output(self, timeout):
        if select.select([self.master], [], [], timeout)[0]:
            try:
                self.transcript += os.read(self.master, 65536)
            except OSError as exc:
                if exc.errno != errno.EIO:
                    raise

    def wait_for(self, marker):
        deadline = time.monotonic() + 5
        while marker not in self.transcript and time.monotonic() < deadline:
            self.read_output(0.05)
            if self.process.poll() is not None:
                break
        self.assertIn(marker, self.transcript, self.transcript.decode(errors="replace"))

    def send(self, data, fragmented=False):
        if fragmented:
            for byte in data:
                os.write(self.master, bytes([byte]))
                time.sleep(0.04)
        else:
            os.write(self.master, data)
        self.read_output(0.15)
        self.assertIsNone(self.process.poll(), "Arrow input exited the TUI")

    def check_navigation(self, sequence, grouped, fragmented=False):
        self.launch(grouped)
        if grouped:
            self.send(sequence, fragmented)
            self.send(b"\r")
            self.wait_for(b"Beta one")
        self.send(sequence, fragmented)
        os.write(self.master, b"o")
        self.wait_for(b"PICKED beta-2" if grouped else b"PICKED alpha-2")

    def test_normal_down_in_flat_list(self):
        self.check_navigation(b"\x1b[B", grouped=False)

    def test_normal_down_in_group_overview_and_scoped_list(self):
        self.check_navigation(b"\x1b[B", grouped=True)

    def test_application_down_in_group_overview_and_scoped_list(self):
        self.check_navigation(b"\x1bOB", grouped=True)

    def test_fragmented_normal_down_in_group_overview_and_scoped_list(self):
        self.check_navigation(b"\x1b[B", grouped=True, fragmented=True)

    def test_up_and_down_preserve_selection(self):
        self.launch(False)
        self.send(b"\x1b[B")
        self.send(b"\x1b[A")
        os.write(self.master, b"o")
        self.wait_for(b"PICKED alpha-1")



if __name__ == "__main__":
    unittest.main()
