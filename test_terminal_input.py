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
    def launch(self, grouped, auto_expand=False):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        config_dir = Path(tmp.name) / "attention"
        config_dir.mkdir()
        # An action's own output never reaches the terminal -- the dashboard
        # owns it -- so the fixture records its pick in a file the test reads,
        # and the page itself only gains a notice.
        self.hits = Path(tmp.name) / "hits"
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
                    sys.executable, "-c",
                    "import pathlib, sys; pathlib.Path(sys.argv[1]).write_text(sys.argv[2])",
                    str(self.hits), "{id}",
                ]}],
            }},
        }
        if grouped:
            config["dashboard"] = {"groups": [
                {"name": "Alpha", "match": {"kinds": ["Alpha"]}},
                {"name": "Beta", "fallback": True},
            ]}
            if auto_expand:
                config["dashboard"]["autoExpand"] = True
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

    def wait_for_action(self, item_id):
        """Wait for the dashboard's own notice, then read what the action did."""
        self.wait_for(b"Done: select")
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if self.hits.exists() and self.hits.read_text() == item_id:
                return
            self.read_output(0.05)
        self.fail(f"action on {item_id!r} recorded {self.hits.read_text() if self.hits.exists() else 'nothing'!r}")

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
        self.send(sequence, fragmented)
        os.write(self.master, b"o")
        self.wait_for_action("beta-2" if grouped else "alpha-2")

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
        self.wait_for_action("alpha-1")

    def test_groups_expand_and_collapse_without_leaving_the_tree(self):
        self.launch(True)
        self.send(b"\r")
        self.wait_for(b"Alpha one")
        self.send(b"\r")
        self.send(b"\x1b[B")
        self.send(b"\r")
        self.wait_for(b"Beta one")
        self.send(b"\x1b[B")
        os.write(self.master, b"o")
        self.wait_for_action("beta-1")

    def test_auto_expand_opens_group_items_without_toggling(self):
        self.launch(True, auto_expand=True)
        self.wait_for(b"Alpha one")
        self.wait_for(b"Beta two")
        self.send(b"\x1b[B")
        os.write(self.master, b"o")
        self.wait_for_action("alpha-1")

    def test_linked_action_disclosure_and_hidden_hotkey(self):
        script = (
            "import base64,json,curses,dashboard\n"
            "a=[{'key':'o','label':'open'},{'key':'1','label':'comment (linked)','_original_key':'c'}]\n"
            "blob=base64.b64encode(json.dumps(a).encode()).decode()\n"
            "p=dashboard.CursesPresenter();p.push_snapshot(['Item\\t'+blob+'\\to,1\\to open'],[])\n"
            "r=curses.wrapper(p._run);print('RESULT',r.key,flush=True)\n"
        )
        master, slave = pty.openpty()
        self.addCleanup(os.close, master)
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 100, 0, 0))
        env = dict(os.environ, TERM="xterm-256color", PYTHONDONTWRITEBYTECODE="1")
        try:
            self.process = subprocess.Popen([sys.executable, "-c", script], stdin=slave,
                                            stdout=slave, stderr=slave, env=env, cwd=ROOT)
        finally:
            os.close(slave)
        self.master, self.transcript = master, b""
        self.addCleanup(self.stop_process)
        self.wait_for(b"show linked actions")
        self.assertNotIn(b"comment (linked)", self.transcript)
        self.send(b"?")
        self.wait_for(b"comment (linked)")
        self.transcript = b""
        self.send(b"?")
        self.wait_for(b"? show")
        os.write(self.master, b"1")
        self.wait_for(b"RESULT 1")

    def spawn(self, build_config):
        """Start the dashboard on a config the caller builds for its temp dir."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        config_dir = self.root / "attention"
        config_dir.mkdir()
        (config_dir / "config.json").write_text(json.dumps(build_config(self.root)))
        master, slave = pty.openpty()
        self.addCleanup(os.close, master)
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 100, 0, 0))
        env = dict(os.environ, TERM="xterm-256color", XDG_CONFIG_HOME=tmp.name,
                   XDG_STATE_HOME=tmp.name, XDG_CACHE_HOME=tmp.name,
                   PYTHONDONTWRITEBYTECODE="1")
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

    @staticmethod
    def generic_config(records, actions):
        return {
            "plugins": ["generic"],
            "dashboard": {"autoExpand": True},
            "generic": {"fixture": {
                "command": [sys.executable, "-c",
                            "print(" + repr(json.dumps(records)) + ")"],
                "title": "{title}", "id": "{id}", "context": "Work",
                "status": "TODO", "weight": "{weight}", "actions": actions,
            }},
        }

    def test_action_prompts_are_answered_without_leaving_the_dashboard(self):
        def build(root):
            out = root / "comment"
            return self.generic_config(
                [{"id": "one", "title": "Alpha one", "weight": 1}],
                [{
                    "key": "c", "label": "comment",
                    "command": [
                        sys.executable, "-c",
                        "import pathlib, sys; pathlib.Path(sys.argv[1]).write_text(sys.argv[2])",
                        str(out), "{input}",
                    ],
                    "input": {"prompt": "Comment body", "default": "fallback"},
                }],
            )

        self.spawn(build)
        self.wait_for(b"Alpha one")
        self.send(b"\x1b[B")
        self.send(b"c")
        # The prompt is drawn by the dashboard itself: no shell, no new
        # terminal window, and the rows stay on screen behind it.
        self.wait_for(b"Comment body")
        self.wait_for(b"Esc cancel")
        self.send(b"typed answer")
        self.send(b"\r")
        self.wait_for(b"Done: comment")
        self.assertEqual((self.root / "comment").read_text(), "typed answer")

    def test_dismissal_removes_the_row_before_the_action_finishes(self):
        def build(root):
            out = root / "completed"
            return self.generic_config(
                [{"id": "one", "title": "Finish me", "weight": 5},
                 {"id": "two", "title": "Stays put", "weight": 1}],
                [{
                    "key": "d", "label": "dismiss", "removes": True,
                    "command": [
                        sys.executable, "-c",
                        "import pathlib, sys, time; time.sleep(1); "
                        "pathlib.Path(sys.argv[1]).write_text(sys.argv[2])",
                        str(out), "{id}",
                    ],
                }],
            )

        self.spawn(build)
        self.wait_for(b"Finish me")
        self.wait_for(b"Stays put")
        self.send(b"\x1b[B")
        self.send(b"d")
        self.wait_for(b"Running: dismiss")
        # The row goes as soon as the dismissal is dispatched, a full second
        # before the command it is waiting on finishes. Curses only writes the
        # cells that change, so move the selection to make it repaint the
        # remaining rows and read what is actually on screen now.
        self.transcript = b""
        self.send(b"\x1b[B")
        self.assertIn(b"Stays put", self.transcript)
        self.assertNotIn(b"Finish me", self.transcript)
        self.wait_for(b"Done: dismiss")
        self.assertEqual((self.root / "completed").read_text(), "one")

    def test_dashboard_renders_stacked_rows_with_individual_glyphs(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        config_dir = root / "attention"
        config_dir.mkdir()
        plugin = root / "fixture.py"
        plugin.write_text(
            "def fetch(config):\n"
            "    return [\n"
            "        {'id':'2','identity_key':'pr:2','parent_identity_key':'pr:1',"
            "'title':'Child','context':'repo','status':'Blocked','details':'','kind':'pull_request',"
            "'indicators':{'state':'Blocked'},'weight':2},\n"
            "        {'id':'1','identity_key':'pr:1','title':'Parent','context':'repo',"
            "'status':'Ready','details':'','kind':'pull_request','indicators':{'state':'Ready'},'weight':1},\n"
            "    ]\n"
        )
        (config_dir / "config.json").write_text(json.dumps({"plugins": [str(plugin)]}))
        master, slave = pty.openpty()
        self.addCleanup(os.close, master)
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 100, 0, 0))
        env = dict(os.environ, TERM="xterm-256color", XDG_CONFIG_HOME=tmp.name,
                   XDG_STATE_HOME=tmp.name, XDG_CACHE_HOME=tmp.name,
                   PYTHONDONTWRITEBYTECODE="1")
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
        self.wait_for(b"Pull Requests (2)")
        self.send(b"\r")
        self.wait_for("└─ Child".encode())
        self.wait_for("✓".encode())
        self.wait_for("✗".encode())
        self.assertNotIn(b"Ready", self.transcript)
        self.assertNotIn(b"Blocked", self.transcript)



if __name__ == "__main__":
    unittest.main()
