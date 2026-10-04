#!/usr/bin/env python3
"""Agent of Empires worker publishing attention items into session panes."""
import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path
import select
import sys
import time

ATTENTION_PATH = Path(__file__).resolve().with_name("attention")
REFRESH_SECONDS = 60
MAX_PANE_BYTES = 64 * 1024
_input_buffer = bytearray()


def send(message):
    sys.stdout.write(json.dumps(message, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def read_message(timeout=None):
    while True:
        newline = _input_buffer.find(b"\n")
        if newline >= 0:
            line = bytes(_input_buffer[:newline])
            del _input_buffer[:newline + 1]
            return json.loads(line)
        ready, _, _ = select.select([sys.stdin.fileno()], [], [], timeout)
        if not ready:
            return None
        chunk = os.read(sys.stdin.fileno(), 4096)
        if not chunk:
            raise EOFError
        _input_buffer.extend(chunk)


def rpc(method, params=None):
    request_id = rpc.next_id
    rpc.next_id += 1
    send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}})
    while True:
        message = read_message()
        if "method" in message:
            dispatch(message)
            continue
        if message.get("id") == request_id:
            if "error" in message:
                raise RuntimeError(message["error"].get("message", "host RPC failed"))
            return message.get("result", {})


rpc.next_id = 1


def attention_items():
    name = "_attention_plugin_" + str(time.monotonic_ns())
    loader = importlib.machinery.SourceFileLoader(name, str(ATTENTION_PATH))
    module = importlib.util.module_from_spec(importlib.util.spec_from_loader(name, loader))
    sys.modules[name] = module
    try:
        loader.exec_module(module)
        return module.build_prioritized_items(module.load_config())
    finally:
        sys.modules.pop(name, None)


def pane_payload(items):
    rows = [
        {
            "kind": "row",
            "label": " · ".join(value for value in (item.get("status", ""), item.get("context", ""), item.get("title", "")) if value)[:300] or "Attention item",
            "value": item.get("details", "")[:500],
        }
        for item in items
    ]
    blocks = [{"kind": "heading", "text": "Prioritized items"}]
    if not rows:
        blocks.append({"kind": "note", "text": "No attention items."})
    blocks.extend(rows)
    refresh = {"kind": "action", "label": "Refresh", "method": "attention.refresh"}
    blocks.append(refresh)
    payload = {"title": "Attention", "default_location": "right", "blocks": blocks}
    omitted = False
    while len(json.dumps(payload, ensure_ascii=False).encode("utf-8")) > MAX_PANE_BYTES and rows:
        blocks.remove(rows.pop())
        omitted = True
    if omitted:
        note = {"kind": "note", "text": "Additional items omitted to fit the pane."}
        blocks.insert(-1, note)
        while len(json.dumps(payload, ensure_ascii=False).encode("utf-8")) > MAX_PANE_BYTES and rows:
            blocks.remove(rows.pop())
    return payload
def publish_snapshot():
    result = rpc("sessions.list")
    sessions = result.get("sessions", []) if isinstance(result, dict) else []
    sessions = [session for session in sessions if isinstance(session, dict) and isinstance(session.get("id"), str)]
    live_ids = {session["id"] for session in sessions}
    for stale_id in published_ids - live_ids:
        rpc("ui.state.remove", {"slot": "pane", "id": "attention", "session_id": stale_id})
        published_ids.discard(stale_id)
    try:
        payload = pane_payload(attention_items())
    except Exception as exc:
        payload = {
            "title": "Attention",
            "default_location": "right",
            "blocks": [
                {"kind": "heading", "text": "Prioritized items"},
                {"kind": "note", "tone": "warn", "text": f"Could not load attention items: {str(exc)[:400]}"},
                {"kind": "action", "label": "Refresh", "method": "attention.refresh"},
            ],
        }
    for session in sessions:
        rpc("ui.state.set", {
            "slot": "pane", "id": "attention", "session_id": session["id"], "payload": payload,
        })
        published_ids.add(session["id"])


def dispatch(message):
    method = message.get("method", "")
    request_id = message.get("id")
    if method == "attention.refresh":
        try:
            publish_snapshot()
            result = {"ok": True}
        except Exception as exc:
            result = {"ok": False, "error": str(exc)[:400]}
    else:
        result = {"ok": False, "error": f"Unsupported method: {method}"}
    if request_id is not None:
        send({"jsonrpc": "2.0", "id": request_id, "result": result})


def main():
    global published_ids
    published_ids = set()
    while True:
        try:
            publish_snapshot()
            deadline = time.monotonic() + REFRESH_SECONDS
            while time.monotonic() < deadline:
                message = read_message(deadline - time.monotonic())
                if message is None:
                    break
                if "method" in message:
                    dispatch(message)
        except EOFError:
            return
        except Exception as exc:
            print(f"attention AoE worker: {exc}", file=sys.stderr, flush=True)
            time.sleep(REFRESH_SECONDS)


if __name__ == "__main__":
    main()
