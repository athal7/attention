#!/usr/bin/env python3
"""Behavioral tests for the Agent of Empires pane worker."""
import importlib.util
import json
import os
from pathlib import Path
import select
import subprocess
import sys
import tempfile
import time
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
        self.assertEqual(payload["blocks"][-1]["method"], "attention.refresh")

    def test_cards_show_status_source_details_and_only_safe_tokenized_actions(self):
        item = {
            "_plugin": "github", "status": "OVERDUE", "context": "private-repo",
            "title": "Ship release", "details": "Review required", "weight": 99,
            "indicators": {"CI": "passing"},
            "actions": [
                {"_plugin": "github", "key": "o", "label": "Open", "primary": True,
                 "payload": {"kind": "open", "url": "https://private.example/issue", "token": "secret-token"}},
                {"_plugin": "github", "key": "m", "label": "Merge", "payload": {"kind": "merge", "command": "secret-merge"}},
                {"_plugin": "github", "key": "c", "label": "Comment", "payload": {"kind": "comment"}},
                {"_plugin": "generic", "key": "1", "label": "Run", "payload": {"command": ["echo", "secret-command"]}},
                {"_plugin": "generic", "key": "2", "label": "Prompt", "payload": {"command": ["echo"], "inputs": [{"prompt": "secret-prompt"}]}},
            ],
        }
        tokens = iter(["opaque-open", "opaque-run"])
        payload, actions = worker.build_pane([item], token_factory=lambda: next(tokens))
        cards = [block for block in payload["blocks"] if block["kind"] == "section"]
        self.assertEqual(len(cards), 1)
        card = cards[0]
        self.assertEqual((card["title"], card["icon"], card["tone"], card["boxed"]),
                         ("Ship release", "github", "danger", True))
        self.assertTrue(any(child.get("label") == "OVERDUE" and child.get("tone") == "danger"
                            for child in card["children"]))
        self.assertTrue(any(child.get("label") == "Details" and child.get("value") == "Review required"
                            for child in card["children"]))
        buttons = [child for child in card["children"] if child.get("method") == "attention.action"]
        self.assertEqual([button["label"] for button in buttons], ["Open", "Run"])
        self.assertEqual([button["params"] for button in buttons],
                         [{"token": "opaque-open"}, {"token": "opaque-run"}])
        self.assertEqual(set(actions), {"opaque-open", "opaque-run"})
        rendered = json.dumps(payload)
        for secret in ("private.example", "secret-token", "secret-merge", "secret-command", "secret-prompt"):
            self.assertNotIn(secret, rendered)

    def test_trimming_keeps_priority_prefix_prunes_tokens_and_obeys_host_limit(self):
        items = [{
            "_plugin": "generic", "status": "NOW", "context": "repo",
            "title": f"Item {index}", "details": "x" * 500, "weight": 100 - index,
            "actions": [{"_plugin": "generic", "key": "o", "label": "Open",
                         "payload": {"url": f"private-{index}"}}],
        } for index in range(12)]
        token_number = iter(range(12))
        token_factory = lambda: f"token-{next(token_number)}"
        with patch.object(worker, "MAX_PANE_BYTES", 2400):
            payload, actions = worker.build_pane(items, token_factory=token_factory)
        cards = [block for block in payload["blocks"] if block["kind"] == "section"]
        titles = [card["title"] for card in cards]
        self.assertLess(len(titles), len(items))
        self.assertEqual(titles, [f"Item {index}" for index in range(len(titles))])
        self.assertIn("Additional items omitted", json.dumps(payload))
        visible_tokens = worker._payload_action_tokens(payload)
        self.assertEqual(set(actions), visible_tokens)
        self.assertLess(len(actions), len(items))
        encoded = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        self.assertLessEqual(len(encoded), 2400)
        huge = [{"status": "NOW", "context": "repo", "title": str(i), "details": "界" * 600}
                for i in range(500)]
        with patch.object(worker, "MAX_PANE_BYTES", 64 * 1024):
            encoded = json.dumps(worker.pane_payload(huge), separators=(",", ":")).encode("utf-8")
        self.assertLessEqual(len(encoded), worker.MAX_PANE_BYTES)



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
    def test_blocked_source_fetch_does_not_block_loading_or_refresh(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            attention = root / "attention"
            calls = root / "calls"
            gates = root / "gates"
            gates.mkdir()
            attention.write_text(
                "from pathlib import Path\n"
                "import time\n"
                f"CALLS = Path({str(calls)!r})\n"
                f"GATES = Path({str(gates)!r})\n"
                "def load_config(): return {}\n"
                "def build_prioritized_items(config):\n"
                "    count = int(CALLS.read_text()) + 1 if CALLS.exists() else 1\n"
                "    CALLS.write_text(str(count))\n"
                "    while not (GATES / f'release-{count}').exists(): time.sleep(0.01)\n"
                "    return [{'status':'now','context':'repo','title':f'Fetch {count}','details':'done'}]\n"
            )
            source = (ROOT / "aoe_worker.py").read_text().replace(
                'ATTENTION_PATH = Path(__file__).resolve().with_name("attention")',
                f'ATTENTION_PATH = Path({str(attention)!r})',
            )
            worker_path = root / "aoe_worker.py"
            worker_path.write_text(source)
            proc = subprocess.Popen(
                [sys.executable, str(worker_path)],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
            )

            def receive(timeout=3):
                ready, _, _ = select.select([proc.stdout], [], [], timeout)
                self.assertTrue(ready, "worker did not respond before timeout")
                line = proc.stdout.readline()
                self.assertTrue(line, "worker exited before responding")
                return json.loads(line)

            def reply(request, result=None):
                proc.stdin.write(json.dumps({
                    "jsonrpc": "2.0", "id": request["id"], "result": result or {},
                }) + "\n")
                proc.stdin.flush()

            try:
                listing = receive()
                self.assertEqual(listing["method"], "sessions.list")
                reply(listing, {"sessions": [{"id": "s1"}]})
                loading = receive()
                self.assertEqual(loading["method"], "ui.state.set")
                loading_payload = loading["params"]["payload"]
                self.assertIn("Loading attention items", json.dumps(loading_payload))
                reply(loading)

                deadline = time.monotonic() + 3
                while not calls.exists() and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertTrue(calls.exists(), "source fetch did not start")
                self.assertEqual(calls.read_text(), "1")

                for request_id in ("refresh-1", "refresh-2", "refresh-3"):
                    proc.stdin.write(json.dumps({
                        "jsonrpc": "2.0", "id": request_id,
                        "method": "attention.refresh", "params": {"session_id": "s1"},
                    }) + "\n")
                proc.stdin.flush()
                responses = [receive() for _ in range(3)]
                self.assertEqual([response["id"] for response in responses], ["refresh-1", "refresh-2", "refresh-3"])
                self.assertTrue(all(response["result"]["ok"] for response in responses))

                (gates / "release-1").touch()
                first_result = receive()
                self.assertEqual(first_result["method"], "ui.state.set")
                self.assertEqual(next(block for block in first_result["params"]["payload"]["blocks"] if block["kind"] == "section")["title"], "Fetch 1")
                reply(first_result)

                deadline = time.monotonic() + 3
                while calls.read_text() != "2" and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertEqual(calls.read_text(), "2", "coalesced refresh did not start one follow-up fetch")
                time.sleep(0.1)
                self.assertEqual(calls.read_text(), "2", "refresh requests started duplicate follow-up fetches")

                (gates / "release-2").touch()
                second_result = receive()
                self.assertEqual(second_result["method"], "ui.state.set")
                self.assertEqual(next(block for block in second_result["params"]["payload"]["blocks"] if block["kind"] == "section")["title"], "Fetch 2")
                reply(second_result)
            finally:
                proc.kill()
                proc.wait(timeout=5)
                proc.stdin.close()
                proc.stdout.close()

    def test_transient_host_rpc_error_retries_after_delay(self):
        with tempfile.TemporaryDirectory() as tmp:
            attention = Path(tmp) / "attention"
            attention.write_text("def load_config(): return {}\ndef build_prioritized_items(config): return [{'status':'now','context':'QA','title':'Recovered','details':'ready'}]\n")
            source = (ROOT / "aoe_worker.py").read_text().replace(
                'ATTENTION_PATH = Path(__file__).resolve().with_name("attention")',
                f'ATTENTION_PATH = Path({str(attention)!r})',
            ).replace("REFRESH_SECONDS = 60", "REFRESH_SECONDS = 0.2")
            worker_path = Path(tmp) / "aoe_worker.py"
            worker_path.write_text(source)
            proc = subprocess.Popen(
                [sys.executable, str(worker_path)],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            )

            def receive(timeout=3):
                ready, _, _ = select.select([proc.stdout], [], [], timeout)
                self.assertTrue(ready, "worker did not retry or publish before timeout")
                line = proc.stdout.readline()
                self.assertTrue(line, "worker exited after a transient host error")
                return json.loads(line)

            def reply(request, result=None, error=None):
                response = {"jsonrpc": "2.0", "id": request["id"]}
                if error:
                    response["error"] = {"message": error}
                else:
                    response["result"] = result or {}
                proc.stdin.write(json.dumps(response) + "\n")
                proc.stdin.flush()

            try:
                first = receive()
                self.assertEqual(first["method"], "sessions.list")
                reply(first, error="temporary store error")

                probe = {"jsonrpc": "2.0", "id": "probe", "method": "attention.unknown"}
                proc.stdin.write(json.dumps(probe) + "\n")
                proc.stdin.flush()
                response = receive()
                self.assertEqual(response["id"], "probe")
                self.assertFalse(response["result"]["ok"])

                retry = receive()
                self.assertEqual(retry["method"], "sessions.list")
                reply(retry, {"sessions": [{"id": "s1"}]})
                deadline = time.monotonic() + 3
                while True:
                    loading = receive(timeout=max(0, deadline - time.monotonic()))
                    if loading["method"] == "sessions.list":
                        reply(loading, {"sessions": [{"id": "s1"}]})
                        continue
                    self.assertEqual(loading["method"], "ui.state.set")
                    self.assertIn("Loading attention items", json.dumps(loading["params"]["payload"]))
                    reply(loading, {})
                    break

                deadline = time.monotonic() + 3
                while True:
                    published = receive(timeout=max(0, deadline - time.monotonic()))
                    if published["method"] == "sessions.list":
                        reply(published, {"sessions": [{"id": "s1"}]})
                        continue
                    self.assertEqual(published["method"], "ui.state.set")
                    self.assertEqual(next(block for block in published["params"]["payload"]["blocks"] if block["kind"] == "section")["title"], "Recovered")
                    reply(published, {})
                    break
            finally:
                proc.kill()
                proc.wait(timeout=5)
                proc.stdin.close()
                proc.stdout.close()
                proc.stderr.close()

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
            ).replace("REFRESH_SECONDS = 60", "REFRESH_SECONDS = 0.05")
            worker_path = Path(tmp) / "aoe_worker.py"
            worker_path.write_text(source)
            proc = subprocess.Popen([sys.executable, str(worker_path)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)

            def receive(timeout=3):
                ready, _, _ = select.select([proc.stdout], [], [], timeout)
                self.assertTrue(ready, "worker did not publish before timeout")
                return json.loads(proc.stdout.readline())

            def reply(request, result=None):
                proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": result or {}}) + "\n")
                proc.stdin.flush()

            try:
                listing = receive()
                self.assertEqual(listing["method"], "sessions.list")
                reply(listing, {"sessions": [{"id": "s1"}]})
                loading = receive()
                self.assertEqual(loading["method"], "ui.state.set")
                self.assertIn("Loading attention items", json.dumps(loading["params"]["payload"]))
                reply(loading)

                while True:
                    pushed = receive()
                    if pushed["method"] == "sessions.list":
                        reply(pushed, {"sessions": [{"id": "s1"}]})
                        continue
                    self.assertEqual(pushed["method"], "ui.state.set")
                    params = pushed["params"]
                    self.assertEqual((params["session_id"], params["slot"], params["id"]), ("s1", "pane", "attention"))
                    self.assertEqual(next(block for block in params["payload"]["blocks"] if block["kind"] == "section")["title"], "Ship")
                    self.assertEqual(params["payload"]["blocks"][-1]["method"], "attention.refresh")
                    reply(pushed)
                    break

                stale_check = receive()
                self.assertEqual(stale_check["method"], "sessions.list")
                reply(stale_check, {"sessions": []})
                removed = receive()
                self.assertEqual(removed["method"], "ui.state.remove")
                self.assertEqual(removed["params"]["session_id"], "s1")
            finally:
                proc.kill()
                proc.wait(timeout=5)
                proc.stdin.close()
                proc.stdout.close()


class AttentionActionDispatchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        loader = importlib.machinery.SourceFileLoader("attention_action_test", str(ROOT / "attention"))
        spec = importlib.util.spec_from_loader(loader.name, loader)
        cls.core = importlib.util.module_from_spec(spec)
        loader.exec_module(cls.core)

    def test_dispatch_preserves_plugin_payload_and_wip_transitions(self):
        class Plugin:
            def __init__(self):
                self.calls = []
            def act(self, key, payload):
                self.calls.append((key, payload))
                return True

        plugin = Plugin()
        action = {"_plugin": "generic", "_original_key": "o", "key": "1",
                  "payload": {"url": "private"}, "wip": True, "_wip_id": "generic:item"}
        with patch.object(self.core, "load_plugin", return_value=plugin), \
             patch.object(self.core, "mark_wip_item") as mark, \
             patch.object(self.core, "unmark_wip_item") as unmark:
            self.assertTrue(self.core.dispatch_item_action(action))
            self.assertEqual(plugin.calls, [("o", {"url": "private"})])
            mark.assert_called_once_with("generic:item")
            unmark.assert_not_called()
            action["wip"] = "clear"
            self.assertTrue(self.core.dispatch_item_action(action))
            unmark.assert_called_once_with("generic:item")

    def test_terminal_act_keeps_its_row_key_contract(self):
        import base64
        action = {"key": "o", "_plugin": "generic", "payload": {}}
        line = "row\t" + base64.b64encode(json.dumps([action]).encode()).decode()
        with patch.object(self.core, "dispatch_item_action", return_value=True) as dispatch:
            self.assertTrue(self.core.act("o", line))
        dispatch.assert_called_once_with(action)

    def test_worker_action_cli_keeps_plugin_stdout_off_json_channel(self):
        import io
        import contextlib
        action = {"_plugin": "fake", "key": "o", "payload": {}}
        output = io.StringIO()
        diagnostics = io.StringIO()
        with patch.object(self.core, "dispatch_item_action", side_effect=lambda _: (print("provider output") or True)), \
             patch("sys.stdin", io.StringIO(json.dumps(action))), \
             patch("sys.stderr", diagnostics), contextlib.redirect_stdout(output):
            self.assertEqual(self.core.worker_action_cli(), 0)
        self.assertEqual(json.loads(output.getvalue()), {"ok": True})
        self.assertEqual(diagnostics.getvalue(), "provider output\n")


class WorkerActionProtocolTests(unittest.TestCase):
    def test_action_is_session_scoped_single_use_and_refreshes_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            attention = root / "attention"
            fetches = root / "fetches"
            dispatched = root / "dispatched.json"
            attention.write_text(
                "import json, sys\nfrom pathlib import Path\n"
                f"FETCHES = Path({str(fetches)!r})\nDISPATCHED = Path({str(dispatched)!r})\n"
                "def load_config(): return {}\n"
                "def build_prioritized_items(config):\n"
                "    count = int(FETCHES.read_text()) + 1 if FETCHES.exists() else 1\n"
                "    FETCHES.write_text(str(count))\n"
                "    return [{'status':'NOW','context':'QA','title':f'Fetch {count}','details':'ready',"
                "'weight':10,'_plugin':'generic','actions':[{'key':'o','label':'Run QA','primary':True,"
                "'_plugin':'generic','_original_key':'o','payload':{'command':['echo','private-command']}}]}]\n"
                "if len(sys.argv) > 1 and sys.argv[1] == '__worker_action__':\n"
                "    DISPATCHED.write_text(json.dumps(json.load(sys.stdin)))\n"
                "    print(json.dumps({'ok': True}))\n"
            )
            source = (ROOT / "aoe_worker.py").read_text().replace(
                'ATTENTION_PATH = Path(__file__).resolve().with_name("attention")',
                f'ATTENTION_PATH = Path({str(attention)!r})',
            )
            worker_path = root / "aoe_worker.py"
            worker_path.write_text(source)
            proc = subprocess.Popen([sys.executable, str(worker_path)], stdin=subprocess.PIPE,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

            def receive(timeout=3):
                ready, _, _ = select.select([proc.stdout], [], [], timeout)
                self.assertTrue(ready, "worker did not send a protocol message")
                line = proc.stdout.readline()
                self.assertTrue(line, "worker exited before replying")
                return json.loads(line)

            def reply(request, result=None):
                proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": request["id"],
                                             "result": result or {}}) + "\n")
                proc.stdin.flush()

            def send_action(request_id, token, session_id):
                proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": request_id,
                    "method": "attention.action", "params": {"token": token, "session_id": session_id}}) + "\n")
                proc.stdin.flush()

            def wait_response(request_id, timeout=4):
                deadline = time.monotonic() + timeout
                while time.monotonic() < deadline:
                    message = receive(max(0.01, deadline - time.monotonic()))
                    if message.get("method") == "ui.state.set":
                        reply(message)
                    elif message.get("method") == "sessions.list":
                        reply(message, {"sessions": [{"id": "s1"}]})
                    elif message.get("id") == request_id:
                        return message
                self.fail(f"worker did not reply to {request_id}")

            try:
                listing = receive()
                self.assertEqual(listing["method"], "sessions.list")
                reply(listing, {"sessions": [{"id": "s1"}]})
                loading = receive()
                self.assertEqual(loading["method"], "ui.state.set")
                reply(loading)
                first = receive()
                while first.get("method") != "ui.state.set":
                    self.assertEqual(first.get("method"), "sessions.list")
                    reply(first, {"sessions": [{"id": "s1"}]})
                    first = receive()
                payload = first["params"]["payload"]
                card = next(block for block in payload["blocks"] if block["kind"] == "section")
                button = next(child for child in card["children"] if child.get("method") == "attention.action")
                token = button["params"]["token"]
                self.assertNotIn("private-command", json.dumps(payload))
                reply(first)

                send_action("wrong-session", token, "other")
                wrong = wait_response("wrong-session")
                self.assertFalse(wrong["result"]["ok"])
                self.assertIn("Unknown session", wrong["result"]["error"])

                send_action("run", token, "s1")
                accepted = wait_response("run")
                self.assertTrue(accepted["result"]["ok"])
                send_action("duplicate", token, "s1")
                duplicate = wait_response("duplicate")
                self.assertFalse(duplicate["result"]["ok"])

                deadline = time.monotonic() + 5
                completed_seen = False
                refreshed_seen = False
                while time.monotonic() < deadline and not refreshed_seen:
                    message = receive(max(0.01, deadline - time.monotonic()))
                    if message.get("method") == "sessions.list":
                        reply(message, {"sessions": [{"id": "s1"}]})
                        continue
                    self.assertEqual(message.get("method"), "ui.state.set")
                    state = json.dumps(message["params"]["payload"])
                    completed_seen = completed_seen or "Completed: Run QA" in state
                    if "Fetch 2" in state:
                        refreshed_seen = True
                    reply(message)
                self.assertTrue(dispatched.exists(), "source action child was not invoked")
                self.assertEqual(json.loads(dispatched.read_text())["payload"]["command"],
                                 ["echo", "private-command"])
                self.assertTrue(completed_seen, "pane omitted action completion feedback")
                self.assertTrue(refreshed_seen, "successful action did not refresh the item snapshot")

                send_action("stale", token, "s1")
                stale = wait_response("stale")
                self.assertFalse(stale["result"]["ok"])
                self.assertIn("expired", stale["result"]["error"])
            finally:
                proc.kill()
                proc.wait(timeout=5)
                proc.stdin.close()
                proc.stdout.close()
                proc.stderr.close()


if __name__ == "__main__":
    unittest.main()
