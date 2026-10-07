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

def _receive_json_line(stream, buffer, timeout, timeout_message):
    deadline = time.monotonic() + timeout
    while True:
        newline = buffer.find(b"\n")
        if newline >= 0:
            line = bytes(buffer[:newline])
            del buffer[:newline + 1]
            return json.loads(line)
        remaining = deadline - time.monotonic()
        ready, _, _ = select.select([stream.fileno()], [], [], max(0, remaining))
        if not ready:
            raise AssertionError(timeout_message)
        chunk = os.read(stream.fileno(), 4096)
        if not chunk:
            raise AssertionError("worker exited before replying")
        buffer.extend(chunk)


class PanePayloadTests(unittest.TestCase):
    def test_empty_items_have_explicit_empty_state_and_refresh(self):
        payload = worker.pane_payload([])
        self.assertEqual(payload["default_location"], "right")
        self.assertEqual(payload["blocks"][0], {"kind": "note", "text": "No attention items."})
        self.assertEqual([block["kind"] for block in payload["blocks"]], ["note", "columns"])
        toolbar = payload["blocks"][1]
        self.assertEqual([action["label"] for action in toolbar["children"]], ["Refresh"])
        self.assertEqual(toolbar["children"][0]["method"], "attention.refresh")

    def test_status_notice_precedes_cards_without_repeating_the_pane_heading(self):
        payload = worker.status_payload("Source refresh failed; showing cached items.")
        self.assertEqual(payload["blocks"][0], {
            "kind": "note", "text": "Source refresh failed; showing cached items."
        })
        self.assertEqual([block["kind"] for block in payload["blocks"]], ["note", "columns"])

    def test_cards_are_compact_and_the_primary_row_opens_the_source(self):
        item = {
            "_plugin": "github", "status": "NEEDS ATTENTION", "context": "private-repo",
            "title": "Ship release", "details": "Review required", "weight": 99,
            "indicators": {"state": "CI failing", "CI": "failed"},
            "actions": [
                {"_plugin": "github", "key": "o", "label": "open", "primary": True,
                 "payload": {"kind": "open", "url": "https://example.com/issue", "token": "secret-token"}},
                {"_plugin": "github", "key": "m", "label": "Merge", "payload": {"kind": "merge", "command": "secret-merge"}},
                {"_plugin": "github", "key": "c", "label": "Comment", "payload": {"kind": "comment"}},
                {"_plugin": "generic", "key": "2", "label": "Prompt", "payload": {"command": ["echo"], "inputs": [{"prompt": "secret-prompt"}]}},
                {"_plugin": "generic", "key": "3", "label": "Run", "payload": {"command": ["echo", "secret-command"]}},
                {"_plugin": "github", "key": "l", "label": "Lumen",
                 "payload": {"command": ["/opt/homebrew/bin/lumen", "diff", "https://private.example/issue"]}},
            ],
        }
        tokens = iter(["opaque-run", "opaque-session"])
        payload, actions = worker.build_pane([item], token_factory=lambda: next(tokens))
        card = next(block for block in payload["blocks"] if block["kind"] == "section")
        self.assertEqual(set(card), {"kind", "boxed", "children"})
        self.assertTrue(card["boxed"])
        row = card["children"][0]
        self.assertEqual(row, {
            "kind": "row", "label": "Ship release",
            "sublabel": "NEEDS ATTENTION · private-repo · CI failing",
            "href": "https://example.com/issue",
        })
        button_group = card["children"][1]
        buttons = button_group["children"]
        self.assertEqual([button["label"] for button in buttons], ["Run", "New session"])
        self.assertEqual([button["params"] for button in buttons], [
            {"token": "opaque-run"}, {"token": "opaque-session"},
        ])
        self.assertEqual(buttons[-1]["method"], "attention.new_session")
        self.assertEqual(set(actions), {"opaque-run", "opaque-session"})
        self.assertEqual(actions["opaque-run"], item["actions"][4])
        self.assertEqual(actions["opaque-session"], {
            "_aoe_operation": "new_session", "title": "Attention: Ship release",
        })
        rendered = json.dumps(payload)
        for secret in ("secret-token", "secret-merge", "secret-command", "secret-prompt", "private.example"):
            self.assertNotIn(secret, rendered)

    def test_own_pull_request_hides_approve_and_keeps_another_supported_action(self):
        item = {
            "_plugin": "github", "title": "Fix release",
            "actions": [
                {"_plugin": "github", "label": "approve", "_is_own_pr": True, "payload": {"kind": "approve"}},
                {"_plugin": "generic", "label": "Run check", "payload": {"command": ["echo", "check"]}},
            ],
        }
        tokens = iter(["run-token", "session-token"])
        payload, actions = worker.build_pane([item], token_factory=lambda: next(tokens))
        card = next(block for block in payload["blocks"] if block["kind"] == "section")
        buttons = card["children"][1]["children"]
        self.assertEqual([button["label"] for button in buttons], ["Run check", "New session"])
        self.assertNotIn("approve", json.dumps(payload).casefold())
        self.assertEqual(set(actions), {"run-token", "session-token"})

    def test_each_item_shows_at_most_one_source_action_plus_new_session(self):
        item = {
            "title": "Build",
            "actions": [
                {"label": label, "payload": {"command": ["echo", label]}}
                for label in ("First", "Second", "Third")
            ],
        }
        tokens = iter(["first-token", "session-token"])
        payload, actions = worker.build_pane([item], token_factory=lambda: next(tokens))
        card = next(block for block in payload["blocks"] if block["kind"] == "section")
        buttons = card["children"][1]["children"]
        self.assertEqual([button["label"] for button in buttons], ["First", "New session"])
        self.assertLessEqual(len(buttons), 2)
        self.assertEqual(set(actions), {"first-token", "session-token"})

    def test_command_based_open_uses_the_linked_row_without_an_open_button(self):
        open_action = {
            "_plugin": "generic", "key": "open", "label": "Open",
            "payload": {"command": ["open", "https://example.com/build"]},
        }
        tokens = iter(["open-token", "session-token"])
        payload, actions = worker.build_pane(
            [{"title": "Build failed", "actions": [open_action]}],
            token_factory=lambda: next(tokens),
        )
        card = next(block for block in payload["blocks"] if block["kind"] == "section")
        row = card["children"][0]
        self.assertEqual(row["method"], "attention.action")
        self.assertEqual(row["params"], {"token": "open-token"})
        buttons = card["children"][1]["children"]
        self.assertEqual([button["label"] for button in buttons], ["New session"])
        self.assertEqual(actions["open-token"], open_action)

    def test_unsafe_source_urls_are_not_rendered_as_links(self):
        payload = worker.pane_payload([{"title": "Unsafe", "url": "javascript:alert(1)"}])
        row = next(block for block in payload["blocks"] if block["kind"] == "section")["children"][0]
        self.assertNotIn("href", row)

    def test_summary_deduplicates_status_context_and_reason(self):
        payload = worker.pane_payload([{
            "status": "OVERDUE", "context": "Accounting", "title": "Pay invoice",
            "attention_reason": " overdue ", "details": "OVERDUE",
        }])
        card = next(block for block in payload["blocks"] if block["kind"] == "section")
        self.assertEqual(card["children"][0]["sublabel"], "OVERDUE · Accounting")
        self.assertNotIn("Details", json.dumps(card))

    def test_trimming_keeps_priority_prefix_prunes_tokens_and_obeys_host_limit(self):
        items = [{
            "_plugin": "generic", "status": "NOW", "context": "repo",
            "title": f"Item {index}", "details": "x" * 500, "weight": 100 - index,
            "actions": [{"_plugin": "generic", "key": "o", "label": "Open",
                         "payload": {"url": f"https://example.com/{index}"}}],
        } for index in range(12)]
        token_number = iter(range(24))
        token_factory = lambda: f"token-{next(token_number)}"
        with patch.object(worker, "MAX_PANE_BYTES", 2400):
            payload, actions = worker.build_pane(items, token_factory=token_factory)
        cards = [block for block in payload["blocks"] if block["kind"] == "section"]
        titles = [card["children"][0]["label"] for card in cards]
        self.assertLess(len(titles), len(items))
        self.assertEqual(titles, [f"Item {index}" for index in range(len(titles))])
        self.assertIn("Additional items omitted", json.dumps(payload))
        visible_tokens = worker._payload_action_tokens(payload)
        self.assertEqual(set(actions), visible_tokens)
        self.assertLess(len(actions), 2 * len(items))
        encoded = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        self.assertLessEqual(len(encoded), 2400)
        huge = [{"status": "NOW", "context": "repo", "title": str(i), "details": "界" * 600}
                for i in range(500)]
        with patch.object(worker, "MAX_PANE_BYTES", 64 * 1024):
            encoded = json.dumps(worker.pane_payload(huge), separators=(",", ":")).encode("utf-8")
        self.assertLessEqual(len(encoded), worker.MAX_PANE_BYTES)

class SessionCreationTests(unittest.TestCase):
    def test_manifest_requests_unattended_session_creation(self):
        import tomllib

        manifest = tomllib.loads((ROOT / "aoe-plugin.toml").read_text())
        self.assertIn("session.create", manifest["capabilities"])
        self.assertIn("session.unattended", manifest["capabilities"])

    def test_missing_agent_setting_does_not_request_session_creation(self):
        request = {
            "token": "single-use-token",
            "session_id": "s1",
            "action": {"title": "Attention: Review PR"},
        }
        with patch.object(worker, "rpc", return_value={"value": None}) as rpc:
            with self.assertRaisesRegex(RuntimeError, "Choose an ACP agent"):
                worker.create_session(request)
        rpc.assert_called_once_with("config.get", {"key": "agent_id"})


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


class InteractiveActionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        loader = importlib.machinery.SourceFileLoader("attention_util_test", str(ROOT / "sources" / "_util.py"))
        spec = importlib.util.spec_from_loader(loader.name, loader)
        cls.util = importlib.util.module_from_spec(spec)
        loader.exec_module(cls.util)

    def test_configured_terminal_action_uses_the_shared_action_runner(self):
        command = ["lumen", "diff", "https://example.com/compare/main; touch /tmp/marker"]
        with patch.object(self.util, "run_terminal", return_value=True) as run_terminal, \
             patch.object(self.util, "run_cmd") as run_cmd:
            self.assertTrue(self.util.run_configured_action({"command": command, "terminal": True}))
        run_terminal.assert_called_once_with(command)
        run_cmd.assert_not_called()

    def test_macos_terminal_shell_quotes_configured_arguments(self):
        url = "https://example.com/compare/main; touch /tmp/marker"
        with patch.object(self.util.sys, "platform", "darwin"), \
             patch.object(self.util.subprocess, "run") as run:
            self.assertTrue(self.util.run_terminal(["lumen", "diff", url]))
        args = run.call_args.args[0]
        self.assertEqual(args[:2], ["osascript", "-e"])
        self.assertIn("lumen diff 'https://example.com/compare/main; touch /tmp/marker'", args[2])
        run.assert_called_once()


    def test_linux_terminal_passes_argv_without_shell_reparsing(self):
        command = ["lumen", "diff", "https://example.com/change; touch /tmp/marker"]
        with patch.object(self.util.sys, "platform", "linux"), \
             patch.dict(os.environ, {"TERMINAL": "custom-term --window"}), \
             patch.object(self.util.subprocess, "Popen") as popen:
            self.assertTrue(self.util.run_terminal(command))
        popen.assert_called_once_with(
            ["custom-term", "--window", "-e", *command], start_new_session=True,
        )

    def test_windows_terminal_receives_argv_without_a_shell(self):
        command = ["lumen", "diff", "https://example.com/change; touch marker"]
        with patch.object(self.util.sys, "platform", "win32"), \
             patch.object(self.util.shutil, "which", return_value="C:/Windows/wt.exe"), \
             patch.object(self.util.subprocess, "Popen") as popen:
            self.assertTrue(self.util.run_terminal(command))
        popen.assert_called_once_with(["C:/Windows/wt.exe", "new-tab", "--", *command])


class WorkerProtocolTests(unittest.TestCase):
    def test_new_session_action_creates_and_confirms_a_host_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            attention = root / "attention"
            attention.write_text(
                "def load_config(): return {}\n"
                "def build_prioritized_items(config):\n"
                "    return [{'status':'now','context':'repo','title':'Fix regression','details':'ready'}]\n"
            )
            source = (ROOT / "aoe_worker.py").read_text().replace(
                'ATTENTION_PATH = Path(__file__).resolve().with_name("attention")',
                f'ATTENTION_PATH = Path({str(attention)!r})',
            )
            worker_path = root / "aoe_worker.py"
            worker_path.write_text(source)
            proc = subprocess.Popen(
                [sys.executable, str(worker_path)],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
            stdout_buffer = bytearray()

            def receive(timeout=3):
                return _receive_json_line(proc.stdout, stdout_buffer, timeout, "worker did not complete session creation")

            def reply(request, result=None):
                proc.stdin.write((json.dumps({
                    "jsonrpc": "2.0", "id": request["id"], "result": result or {},
                }) + "\n").encode())
                proc.stdin.flush()

            try:
                listing = receive()
                self.assertEqual(listing["method"], "sessions.list")
                reply(listing, {"sessions": [{"id": "s1", "project_path": "/repo"}]})

                loading = receive()
                self.assertEqual(loading["method"], "ui.state.set")
                reply(loading)

                snapshot = receive()
                self.assertEqual(snapshot["method"], "ui.state.set")
                payload = snapshot["params"]["payload"]
                button = next(
                    block for block in worker._walk_blocks(payload["blocks"])
                    if block.get("label") == "New session"
                )
                self.assertEqual(button["method"], "attention.new_session")
                reply(snapshot)

                action_id = "create-session-action"
                proc.stdin.write((json.dumps({
                    "jsonrpc": "2.0", "id": action_id, "method": button["method"],
                    "params": {**button["params"], "session_id": "s1"},
                }) + "\n").encode())
                proc.stdin.flush()
                accepted = receive()
                self.assertEqual(accepted["id"], action_id)
                self.assertTrue(accepted["result"]["ok"])

                creating = receive()
                self.assertEqual(creating["method"], "ui.state.set")
                self.assertIn("Creating session: Attention: Fix regression", json.dumps(creating["params"]["payload"]))
                reply(creating)

                settings = receive()
                self.assertEqual((settings["method"], settings["params"]), ("config.get", {"key": "agent_id"}))
                reply(settings, {"value": "omp"})

                create = receive()
                self.assertEqual(create["method"], "sessions.create")
                self.assertEqual(create["params"]["agent_id"], "omp")
                self.assertEqual(create["params"]["title"], "Attention: Fix regression")
                self.assertEqual(create["params"]["project_path"], "/repo")
                self.assertEqual(create["params"]["idempotency_key"], button["params"]["token"])
                reply(create, {"session_id": "created-session", "created": True})

                completed = receive()
                self.assertEqual(completed["method"], "ui.state.set")
                self.assertIn("Created session: created-session", json.dumps(completed["params"]["payload"]))
                self.assertEqual(completed["params"]["session_id"], "s1")
                reply(completed)
            finally:
                proc.kill()
                proc.wait(timeout=5)
                proc.stdin.close()
                proc.stdout.close()
                proc.stderr.close()

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

            stdout_buffer = bytearray()

            def receive(timeout=3):
                return _receive_json_line(proc.stdout, stdout_buffer, timeout, "worker did not respond before timeout")

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
                self.assertEqual(next(block for block in first_result["params"]["payload"]["blocks"] if block["kind"] == "section")["children"][0]["label"], "Fetch 1")
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
                self.assertEqual(next(block for block in second_result["params"]["payload"]["blocks"] if block["kind"] == "section")["children"][0]["label"], "Fetch 2")
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

            stdout_buffer = bytearray()

            def receive(timeout=3):
                return _receive_json_line(proc.stdout, stdout_buffer, timeout, "worker did not retry or publish before timeout")

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
                    self.assertEqual(next(block for block in published["params"]["payload"]["blocks"] if block["kind"] == "section")["children"][0]["label"], "Recovered")
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

            stdout_buffer = bytearray()

            def receive(timeout=3):
                return _receive_json_line(proc.stdout, stdout_buffer, timeout, "worker did not publish before timeout")

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
                    self.assertEqual(next(block for block in params["payload"]["blocks"] if block["kind"] == "section")["children"][0]["label"], "Ship")
                    toolbar = params["payload"]["blocks"][-1]
                    self.assertEqual(toolbar["kind"], "columns")
                    self.assertEqual(toolbar["children"][-1]["method"], "attention.refresh")
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
                  "payload": {"url": "private", "terminal": True}, "wip": True, "_wip_id": "generic:item"}
        with patch.object(self.core, "load_plugin", return_value=plugin), \
             patch.object(self.core, "mark_wip_item") as mark, \
             patch.object(self.core, "unmark_wip_item") as unmark:
            self.assertTrue(self.core.dispatch_item_action(action))
            self.assertEqual(plugin.calls, [("o", {"url": "private", "terminal": True})])
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
            created_params = []
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

            stdout_buffer = bytearray()

            def receive(timeout=3):
                return _receive_json_line(proc.stdout, stdout_buffer, timeout, "worker did not send a protocol message")

            def reply(request, result=None):
                proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": request["id"],
                                             "result": result or {}}) + "\n")
                proc.stdin.flush()

            def send_action(request_id, token, session_id, method="attention.action"):
                proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": request_id,
                    "method": method, "params": {"token": token, "session_id": session_id}}) + "\n")
                proc.stdin.flush()

            def wait_response(request_id, timeout=4):
                deadline = time.monotonic() + timeout
                while time.monotonic() < deadline:
                    message = receive(max(0.01, deadline - time.monotonic()))
                    if message.get("method") == "ui.state.set":
                        reply(message)
                    elif message.get("method") == "sessions.list":
                        reply(message, {"sessions": [{"id": "s1", "project_path": "/repo"}]})
                    elif message.get("method") == "config.get":
                        reply(message, {"value": "omp"})
                    elif message.get("method") == "sessions.create":
                        created_params.append(message["params"])
                        reply(message, {"session_id": "created-session", "created": True})
                    elif message.get("id") == request_id:
                        return message
                self.fail(f"worker did not reply to {request_id}")

            try:
                listing = receive()
                self.assertEqual(listing["method"], "sessions.list")
                reply(listing, {"sessions": [{"id": "s1", "project_path": "/repo"}]})
                loading = receive()
                self.assertEqual(loading["method"], "ui.state.set")
                reply(loading)
                first = receive()
                while first.get("method") != "ui.state.set":
                    self.assertEqual(first.get("method"), "sessions.list")
                    reply(first, {"sessions": [{"id": "s1", "project_path": "/repo"}]})
                    first = receive()
                payload = first["params"]["payload"]
                card = next(block for block in payload["blocks"] if block["kind"] == "section")
                button_group = next(child for child in card["children"] if child.get("kind") == "columns")
                button = next(child for child in button_group["children"] if child.get("method") == "attention.action")
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
                new_session_token = None
                while time.monotonic() < deadline and not refreshed_seen:
                    message = receive(max(0.01, deadline - time.monotonic()))
                    if message.get("method") == "sessions.list":
                        reply(message, {"sessions": [{"id": "s1", "project_path": "/repo"}]})
                        continue
                    self.assertEqual(message.get("method"), "ui.state.set")
                    payload = message["params"]["payload"]
                    state = json.dumps(payload)
                    completed_seen = completed_seen or "Completed: Run QA" in state
                    if "Fetch 2" in state:
                        refreshed_seen = True
                        action = next(block for block in worker._walk_blocks(payload["blocks"])
                                      if block.get("method") == "attention.new_session")
                        new_session_token = action["params"]["token"]
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
                self.assertIsNotNone(new_session_token)
                send_action("create-session", new_session_token, "s1", method="attention.new_session")
                accepted = wait_response("create-session")
                self.assertTrue(accepted["result"]["ok"])
                created_notice_seen = False
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline and not created_notice_seen:
                    message = receive(max(0.01, deadline - time.monotonic()))
                    if message.get("method") == "ui.state.set":
                        created_notice_seen = "Created session: created-session" in json.dumps(message["params"]["payload"])
                        reply(message)
                    elif message.get("method") == "config.get":
                        self.assertEqual(message["params"], {"key": "agent_id"})
                        reply(message, {"value": "omp"})
                    elif message.get("method") == "sessions.create":
                        created_params.append(message["params"])
                        reply(message, {"session_id": "created-session", "created": True})
                    elif message.get("method") == "sessions.list":
                        reply(message, {"sessions": [{"id": "s1", "project_path": "/repo"}]})
                    else:
                        self.fail(f"unexpected host RPC during create: {message}")
                self.assertTrue(created_notice_seen, "pane omitted session creation result")
                self.assertEqual(created_params, [{
                    "agent_id": "omp", "project_path": "/repo",
                    "title": "Attention: Fetch 2", "idempotency_key": new_session_token,
                }])
                send_action("duplicate-session", new_session_token, "s1", method="attention.new_session")
                duplicate_session = wait_response("duplicate-session")
                self.assertFalse(duplicate_session["result"]["ok"])
                self.assertIn("expired", duplicate_session["result"]["error"])
            finally:
                proc.kill()
                proc.wait(timeout=5)
                proc.stdin.close()
                proc.stdout.close()
                proc.stderr.close()


if __name__ == "__main__":
    unittest.main()
