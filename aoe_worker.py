#!/usr/bin/env python3
"""Agent of Empires worker publishing attention items into session panes."""
import importlib.machinery
import importlib.util
import json
import os
import queue
from pathlib import Path
import secrets
import select
import subprocess
import sys
import threading
import time
ATTENTION_PATH = Path(__file__).resolve().with_name("attention")
REFRESH_SECONDS = 60
MAX_PANE_BYTES = 64 * 1024
ACTION_TIMEOUT_SECONDS = 120
_input_buffer = bytearray()
_source_lock = threading.Lock()


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
        if not isinstance(message, dict):
            continue
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


def _tone_for_status(status):
    value = status.lower()
    if any(word in value for word in ("overdue", "blocked", "failed", "failure", "error", "conflict", "changes requested")):
        return "danger"
    if any(word in value for word in ("urgent", "review", "due", "pending", "requested")):
        return "warn"
    if any(word in value for word in ("approved", "complete", "resolved", "merged", "passing", "healthy", "done")):
        return "success"
    if any(word in value for word in ("open", "in progress", "running")):
        return "info"
    return "neutral"


def _source_icon(plugin):
    return {
        "github": "github",
        "linear": "circle-dot",
        "calendar": "calendar-days",
        "reminders": "square-check",
        "generic": "square-terminal"
    }.get(plugin, "circle-help")


def _status_icon(tone):
    return {"danger": "circle-alert", "warn": "triangle-alert", "success": "circle-check", "info": "circle-dot"}.get(tone, "circle")


def _action_icon(label):
    value = label.lower()
    if "open" in value or "view" in value:
        return "external-link"
    if "approve" in value or "complete" in value or "done" in value:
        return "check"
    if "copy" in value or "yank" in value:
        return "copy"
    if "dismiss" in value or "snooze" in value:
        return "bell-off"
    return "play"


def _web_actions(item, token_actions, token_factory):
    blocks = []
    interactive = {
        "github": {"comment", "label", "merge"},
        "linear": {"comment", "transition"},
    }
    for action in item.get("actions", []):
        if not isinstance(action, dict):
            continue
        plugin = action.get("_plugin") or item.get("_plugin", "")
        payload = action.get("payload")
        if not isinstance(payload, dict) or payload.get("inputs"):
            continue
        if str(payload.get("kind", "")).lower() in interactive.get(plugin, set()):
            continue
        token = token_factory()
        token_actions[token] = dict(action)
        blocks.append({
            "kind": "action",
            "label": action.get("label", "Action")[:80],
            "method": "attention.action",
            "params": {"token": token},
            "icon": _action_icon(action.get("label", "")),
            "variant": "primary" if action.get("primary") else "secondary",
        })
    return blocks


def _item_card(item, token_actions, token_factory):
    status = str(item.get("status", ""))[:100]
    context = str(item.get("context", ""))[:200]
    title = str(item.get("title", "Attention item"))[:240]
    details = str(item.get("details", ""))[:600]
    plugin = str(item.get("_plugin", ""))
    tone = _tone_for_status(status)
    children = [{
        "kind": "row",
        "label": status or "Needs attention",
        "value": context,
        "icon": _status_icon(tone),
        "tone": tone,
    }]
    if details:
        children.append({"kind": "row", "label": "Details", "value": details, "icon": "align-left"})
    indicators = item.get("indicators")
    if isinstance(indicators, dict) and indicators:
        value = " · ".join(f"{key}: {text}" for key, text in indicators.items())[:300]
        children.append({"kind": "row", "label": "Signals", "value": value, "icon": "activity"})
    children.extend(_web_actions(item, token_actions, token_factory))
    return {
        "kind": "section",
        "title": title,
        "icon": _source_icon(plugin),
        "tone": tone,
        "boxed": True,
        "children": children,
    }


def _tokens_in_payload(payload):
    tokens = set()
    for block in payload.get("blocks", []):
        for child in block.get("children", []):
            params = child.get("params", {})
            if child.get("method") == "attention.action" and isinstance(params.get("token"), str):
                tokens.add(params["token"])
    return tokens


def build_pane(items, token_factory=None):
    token_factory = token_factory or (lambda: secrets.token_urlsafe(18))
    token_actions = {}
    cards = [_item_card(item, token_actions, token_factory) for item in items]
    heading = {"kind": "heading", "text": "Prioritized items"}
    refresh = {"kind": "action", "label": "Refresh", "method": "attention.refresh", "icon": "refresh-cw"}
    omission = {"kind": "note", "text": "Additional items omitted to fit the pane."}
    base = {"title": "Attention", "default_location": "right"}
    blocks = [heading]
    if not cards:
        blocks.append({"kind": "note", "text": "No attention items."})
    blocks.extend(cards)
    blocks.append(refresh)
    payload = {**base, "blocks": blocks}
    if len(json.dumps(payload, separators=(",", ":")).encode("utf-8")) <= MAX_PANE_BYTES:
        return payload, token_actions

    kept = []
    blocks = [heading, omission, refresh]
    size = len(json.dumps({**base, "blocks": blocks}, separators=(",", ":")).encode("utf-8"))
    for card in cards:
        card_size = len(json.dumps(card, separators=(",", ":")).encode("utf-8")) + 1
        if size + card_size > MAX_PANE_BYTES:
            break
        kept.append(card)
        size += card_size
    payload = {**base, "blocks": [heading, *kept, omission, refresh]}
    visible_tokens = _tokens_in_payload(payload)
    return payload, {token: action for token, action in token_actions.items() if token in visible_tokens}


def pane_payload(items):
    return build_pane(items)[0]



def live_sessions():
    result = rpc("sessions.list")
    sessions = result.get("sessions", []) if isinstance(result, dict) else []
    sessions = [session for session in sessions if isinstance(session, dict) and isinstance(session.get("id"), str)]
    live_ids = {session["id"] for session in sessions}
    for stale_id in published_ids - live_ids:
        rpc("ui.state.remove", {"slot": "pane", "id": "attention", "session_id": stale_id})
        published_ids.discard(stale_id)
    return sessions


def status_payload(text):
    return {
        "title": "Attention",
        "default_location": "right",
        "blocks": [
            {"kind": "heading", "text": "Prioritized items"},
            {"kind": "note", "text": text},
            {"kind": "action", "label": "Refresh", "method": "attention.refresh", "icon": "refresh-cw"},
        ],
    }


def _payload_action_tokens(payload):
    return {
        child.get("params", {}).get("token")
        for block in payload.get("blocks", [])
        for child in block.get("children", [])
        if child.get("method") == "attention.action"
        and isinstance(child.get("params", {}).get("token"), str)
    }


def payload_with_notice(base_payload, text, consumed_token=None):
    payload = json.loads(json.dumps(base_payload))
    for block in payload["blocks"]:
        children = block.get("children")
        if isinstance(children, list):
            block["children"] = [
                child for child in children
                if not (child.get("method") == "attention.action" and child.get("params", {}).get("token") == consumed_token)
            ]
    blocks = payload["blocks"]
    blocks.insert(1, {"kind": "note", "text": text[:200]})
    omission = {"kind": "note", "text": "Additional items omitted to fit the pane."}
    while len(json.dumps(payload, separators=(",", ":")).encode("utf-8")) > MAX_PANE_BYTES:
        card_index = next((i for i in range(len(blocks) - 1, 0, -1) if blocks[i].get("kind") == "section"), None)
        if card_index is None:
            break
        blocks.pop(card_index)
        if omission not in blocks:
            refresh_index = next((i for i, block in enumerate(blocks) if block.get("method") == "attention.refresh"), len(blocks))
            blocks.insert(refresh_index, omission)
    return payload

def publish_payload(sessions, payload, only_new=False):
    for session in sessions:
        session_id = session["id"]
        if only_new and session_id in published_ids:
            continue
        rpc("ui.state.set", {
            "slot": "pane", "id": "attention", "session_id": session_id, "payload": payload,
        })
        published_ids.add(session_id)


def fetch_snapshot(results):
    try:
        with _source_lock:
            payload, actions = build_pane(attention_items())
        results.put((payload, actions, None))
    except Exception as exc:
        results.put((None, {}, str(exc)[:400]))


def start_fetch(results):
    threading.Thread(target=fetch_snapshot, args=(results,), daemon=True).start()


def execute_action(token, action, label, results):
    completed = False
    try:
        with _source_lock:
            process = subprocess.run(
                [sys.executable, str(ATTENTION_PATH), "__worker_action__"],
                input=json.dumps(action), text=True, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, timeout=ACTION_TIMEOUT_SECONDS,
            )
        output = process.stdout.splitlines()
        response = json.loads(output[-1]) if output else {}
        completed = process.returncode == 0 and response.get("ok") is True
    except Exception:
        completed = False
    results.put((token, label, completed))


def start_action(token, action, label, results):
    threading.Thread(target=execute_action, args=(token, action, label, results), daemon=True).start()


def dispatch(message):
    global refresh_requested, active_action
    method = message.get("method", "")
    request_id = message.get("id")
    params = message.get("params")
    if not isinstance(params, dict):
        params = {}
    if method == "attention.refresh":
        refresh_requested = True
        result = {"ok": True}
    elif method == "attention.action":
        token = params.get("token")
        session_id = params.get("session_id")
        if session_id not in live_session_ids:
            result = {"ok": False, "error": "Unknown session."}
        elif active_action is not None:
            result = {"ok": False, "error": "Another action is already running."}
        else:
            action = action_tokens.pop(token, None) if isinstance(token, str) else None
            if action is None:
                result = {"ok": False, "error": "This action expired. Refresh the pane and try again."}
            else:
                active_action = {"token": token, "action": action, "session_id": session_id}
                action_requests.put(active_action)
                result = {"ok": True}
    else:
        result = {"ok": False, "error": f"Unsupported method: {method}"}
    if request_id is not None:
        send({"jsonrpc": "2.0", "id": request_id, "result": result})

def main():
    global published_ids, refresh_requested, action_tokens, live_session_ids
    global action_requests, active_action
    published_ids = set()
    refresh_requested = False
    action_tokens = {}
    live_session_ids = set()
    action_requests = queue.Queue()
    action_results = queue.Queue()
    active_action = None
    results = queue.Queue(maxsize=1)
    base_payload = status_payload("Loading attention items…")
    payload = base_payload
    sessions = []
    initialized = False
    fetch_in_flight = False
    action_in_flight = False
    refresh_pending = False
    retry_publish = True
    now = time.monotonic()
    next_refresh = now + REFRESH_SECONDS
    next_sessions = now

    while True:
        try:
            deadline = min(next_refresh, next_sessions)
            message = read_message(max(0, min(0.25, deadline - time.monotonic())))
            if isinstance(message, dict) and "method" in message:
                dispatch(message)

            now = time.monotonic()
            if now >= next_sessions:
                sessions = live_sessions()
                live_session_ids = {session["id"] for session in sessions}
                if not initialized:
                    publish_payload(sessions, payload)
                    initialized = True
                    start_fetch(results)
                    fetch_in_flight = True
                    next_refresh = now + REFRESH_SECONDS
                else:
                    publish_payload(sessions, payload, only_new=not retry_publish)
                retry_publish = False
                next_sessions = now + REFRESH_SECONDS

            try:
                request = action_requests.get_nowait()
            except queue.Empty:
                request = None
            if request is not None:
                label = request["action"].get("label", "Action")
                payload = payload_with_notice(base_payload, f"Running: {label}", request["token"])
                action_tokens = {
                    token: action for token, action in action_tokens.items()
                    if token in _payload_action_tokens(payload)
                }
                publish_payload(sessions, payload)
                start_action(request["token"], request["action"], label, action_results)
                action_in_flight = True

            try:
                finished_token, finished_label, completed = action_results.get_nowait()
            except queue.Empty:
                finished_token = None
            if finished_token is not None and active_action and active_action["token"] == finished_token:
                action_in_flight = False
                active_action = None
                message_text = f"Completed: {finished_label}" if completed else f"Action failed: {finished_label}"
                payload = payload_with_notice(base_payload, message_text)
                action_tokens = {
                    token: action for token, action in action_tokens.items()
                    if token in _payload_action_tokens(payload)
                }
                publish_payload(sessions, payload)
                refresh_requested = True

            if initialized and now >= next_refresh:
                refresh_requested = True
                next_refresh = now + REFRESH_SECONDS
            if initialized and refresh_requested:
                refresh_requested = False
                if fetch_in_flight or action_in_flight:
                    refresh_pending = True
                else:
                    start_fetch(results)
                    fetch_in_flight = True

            try:
                updated_payload, updated_actions, error = results.get_nowait()
            except queue.Empty:
                updated_payload = None
                updated_actions = None
                error = None
            if updated_payload is not None or updated_actions is not None:
                fetch_in_flight = False
                base_payload = updated_payload if error is None else status_payload(
                    f"Could not load attention items: {error}",
                )
                payload = base_payload
                action_tokens = updated_actions if error is None else {}
                publish_payload(sessions, payload)
                if refresh_pending and not action_in_flight:
                    refresh_pending = False
                    start_fetch(results)
                    fetch_in_flight = True
        except EOFError:
            return
        except Exception as exc:
            print(f"attention AoE worker: {exc}", file=sys.stderr, flush=True)
            retry_publish = True
            retry_at = time.monotonic() + REFRESH_SECONDS
            next_sessions = retry_at
            next_refresh = retry_at
            refresh_requested = False


if __name__ == "__main__":
    main()
