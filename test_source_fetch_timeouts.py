#!/usr/bin/env python3
"""Behavioral tests for bounded external Attention source commands."""
import importlib.util
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent
SOURCES = ROOT / "sources"
sys.path.insert(0, str(SOURCES))


def load_source(name, filename):
    spec = importlib.util.spec_from_file_location(name, SOURCES / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


calendar_source = load_source("attention_calendar", "calendar.py")
reminders_source = load_source("attention_reminders", "reminders.py")


def executable(path, source):
    path.write_text("#!/usr/bin/env python3\n" + source)
    path.chmod(0o755)


class SourceFetchTimeoutTests(unittest.TestCase):
    def test_slow_calendar_does_not_prevent_later_calendar_results(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake_ical = Path(tmp) / "ical"
            executable(
                fake_ical,
                "import json, sys, time\n"
                "if sys.argv[3] == 'Slow': time.sleep(2)\n"
                "print(json.dumps([{'title':'Available event','id':'event-1','all_day':True}]))\n",
            )
            with (
                patch.object(calendar_source, "_get_ical_path", return_value=str(fake_ical)),
                patch.object(calendar_source, "FETCH_TIMEOUT_SECONDS", 0.5),
            ):
                started = time.monotonic()
                items = calendar_source.fetch({"calendar": {"names": ["Slow", "Fast"]}})
                elapsed = time.monotonic() - started

        self.assertEqual([item["title"] for item in items], ["Available event"])
        self.assertLess(elapsed, 1.5)

    def test_hung_reminders_command_returns_without_blocking_the_fetch(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake_remindctl = Path(tmp) / "remindctl"
            executable(fake_remindctl, "import time\ntime.sleep(2)\n")
            env_path = os.pathsep.join((tmp, os.environ.get("PATH", "")))
            with (
                patch.dict(os.environ, {"PATH": env_path}),
                patch.object(reminders_source, "FETCH_TIMEOUT_SECONDS", 0.5),
            ):
                started = time.monotonic()
                items = reminders_source.fetch({"reminders": {"lists": ["Home"]}})
                elapsed = time.monotonic() - started

        self.assertEqual(items, [])
        self.assertLess(elapsed, 1.5)


if __name__ == "__main__":
    unittest.main()
