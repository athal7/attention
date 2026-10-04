#!/usr/bin/env python3
"""Behavioral tests for the Agent of Empires pane worker."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location("aoe_worker", ROOT / "aoe_worker.py")
worker = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(worker)


class PanePayloadTests(unittest.TestCase):
    def test_empty_items_have_explicit_empty_state_and_refresh(self):
        payload = worker.pane_payload([])
        self.assertEqual(payload["default_location"], "right")
        self.assertIn({"kind": "note", "text": "No attention items."}, payload["blocks"])
        self.assertIn({"kind": "action", "label": "Refresh", "method": "attention.refresh"}, payload["blocks"])

    def test_items_preserve_priority_order_and_fit_host_limit(self):
        items = [
            {"status": "urgent", "context": "repo", "title": "Fix", "details": "first"},
            {"status": "later", "context": "calendar", "title": "Plan", "details": "second"},
        ]
        payload = worker.pane_payload(items)
        rows = [block for block in payload["blocks"] if block["kind"] == "row"]
        self.assertEqual([block["label"] for block in rows], ["urgent · repo · Fix", "later · calendar · Plan"])
        for details in ("z" * 500, "界" * 500):
            huge = [{"status": "x", "context": "", "title": str(i), "details": details} for i in range(500)]
            encoded = json.dumps(worker.pane_payload(huge), separators=(",", ":")).encode("utf-8")
            self.assertLessEqual(len(encoded), worker.MAX_PANE_BYTES)

    def test_trimming_keeps_largest_ordered_prefix_with_duplicate_rows(self):
        first = {"status": "now", "context": "repo", "title": "Ship", "details": "x" * 500}
        middle = {"status": "later", "context": "calendar", "title": "Plan", "details": "middle"}
        last = dict(first)
        heading = {"kind": "heading", "text": "Prioritized items"}
        omission = {"kind": "note", "text": "Additional items omitted to fit the pane."}
        refresh = {"kind": "action", "label": "Refresh", "method": "attention.refresh"}
        expected_rows = [
            block for block in worker.pane_payload([first, middle])["blocks"]
            if block["kind"] == "row"
        ]
        expected = {"title": "Attention", "default_location": "right", "blocks": [heading, *expected_rows, omission, refresh]}
        limit = len(json.dumps(expected, separators=(",", ":")).encode("utf-8"))
        with patch.object(worker, "MAX_PANE_BYTES", limit):
            payload = worker.pane_payload([first, middle, last])
        rows = [block for block in payload["blocks"] if block["kind"] == "row"]
        self.assertEqual(rows, expected_rows)
        self.assertIn(omission, payload["blocks"])
        self.assertIn(refresh, payload["blocks"])
        self.assertLessEqual(len(json.dumps(payload, separators=(",", ":")).encode("utf-8")), limit)


class AttentionSourceTests(unittest.TestCase):
    def test_real_attention_module_reads_config_without_stale_module_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "attention"
            config.mkdir()
            (config / "config.json").write_text('{"plugins": []}')
            state = Path(tmp) / "state"
            with patch.dict(os.environ, {"XDG_CONFIG_HOME": tmp, "XDG_STATE_HOME": str(state)}):
                with patch.object(worker, "ATTENTION_PATH", ROOT / "attention"):
                    self.assertEqual(worker.attention_items(), [])


class WorkerProtocolTests(unittest.TestCase):
    def test_rpc_ignores_non_object_messages_before_host_response(self):
        request_id = worker.rpc.next_id
        with patch.object(worker, "send") as send, patch.object(
            worker, "read_message",
            side_effect=[None, [], "unexpected", {"id": request_id, "result": {"sessions": []}}],
        ):
            result = worker.rpc("sessions.list")
        self.assertEqual(result, {"sessions": []})
        send.assert_called_once()

    def test_refresh_publishes_live_session_and_removes_stale_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            attention = Path(tmp) / "attention"
            attention.write_text("def load_config(): return {}\ndef build_prioritized_items(config): return [{'status':'now','context':'repo','title':'Ship','details':'review'}]\n")
            source = (ROOT / "aoe_worker.py").read_text().replace(
                'ATTENTION_PATH = Path(__file__).resolve().with_name("attention")',
                f'ATTENTION_PATH = Path({str(attention)!r})',
            )
            worker_path = Path(tmp) / "aoe_worker.py"
            worker_path.write_text(source)
            proc = subprocess.Popen([sys.executable, str(worker_path)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
            try:
                first = json.loads(proc.stdout.readline())
                self.assertEqual(first["method"], "sessions.list")
                proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": first["id"], "result": {"sessions": [{"id": "s1"}]}}) + "\n")
                proc.stdin.flush()
                pushed = json.loads(proc.stdout.readline())
                self.assertEqual(pushed["method"], "ui.state.set")
                params = pushed["params"]
                self.assertEqual((params["session_id"], params["slot"], params["id"]), ("s1", "pane", "attention"))
                self.assertEqual(params["payload"]["blocks"][1]["label"], "now · repo · Ship")
                self.assertEqual(params["payload"]["blocks"][-1]["method"], "attention.refresh")
                proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": pushed["id"], "result": {}}) + "\n")
                proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": "refresh", "method": "attention.refresh", "params": {"session_id": "s1"}}) + "\n")
                proc.stdin.flush()
                second = json.loads(proc.stdout.readline())
                self.assertEqual(second["method"], "sessions.list")
                proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": second["id"], "result": {"sessions": []}}) + "\n")
                proc.stdin.flush()
                removed = json.loads(proc.stdout.readline())
                self.assertEqual(removed["method"], "ui.state.remove")
                self.assertEqual(removed["params"]["session_id"], "s1")
            finally:
                proc.kill()
                proc.wait(timeout=5)
                proc.stdin.close()
                proc.stdout.close()


if __name__ == "__main__":
    unittest.main()
