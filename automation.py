"""automation -- config-driven rules that act on the fetched item list.

Standalone module: no import of `attention` anywhere in this file. Like
`dashboard.py`, core owns the wiring and hands this module the pieces only
it can supply:

- `matches(item, match)` -- the same match selectors `dashboard.groups`
  uses, so an automation rule selects items with one vocabulary.
- `item_key(item)` -- the stable per-item identity a rule has already acted
  on (core's WIP id: provider-qualified where the item has an id).
- `start_session(item, message)` -- start the item's own configured
  session action with `message` pre-filled, returning whether it started.
- `state_path` -- where fired rules and pending nudges are remembered.

A rule is evaluated against the merged, prioritized item list on every
fetch round. It fires at most once per item, or again after its optional
`cooldownMinutes`, so re-rendering the dashboard never repeats work.

Two rule types:

- `session`: hand the item's configured `session`/`New session` action its
  message as `{input}` and dispatch it in the background -- the same
  command a hotkey would run, just without a prompt.
- `nudge`: remember the message and render it as a `NUDGE` row until it is
  dismissed. Pending nudges live in the state file, so they survive across
  runs instead of disappearing with the round that produced them.

Message text is a `{field}` template resolved against the item (the same
syntax configured actions and generic providers already use), defaulting
to the item's `session_prompt` for session rules and its title otherwise.
"""
from __future__ import annotations

import fcntl
import json
import os
import re
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

SECTION_FIELDS = {"rules", "maxWip"}

RULE_FIELDS = {"name", "match", "type", "message", "cooldownMinutes"}
RULE_TYPES = {"session", "nudge"}

NUDGE_STATUS = "NUDGE"
NUDGE_PLUGIN = "automation"

_PLACEHOLDER = re.compile(r"\{([\w.]+)\}")
_RESERVED_PLACEHOLDERS = ("input",)


def load_rules(config, normalize_match):
    """Validate `config["automation"]` and return (rules, error).

    `normalize_match(raw_match, path)` is core's dashboard-group match
    normalizer; a rule keeps its normalized match so evaluation never has
    to re-validate the configuration. Any error discards automation for
    the run rather than half-applying rules.
    """
    section = config.get("automation") if isinstance(config, dict) else None
    if section is None:
        return [], None
    if not isinstance(section, dict):
        return None, "automation configuration must be an object"
    unsupported = set(section) - SECTION_FIELDS
    if unsupported:
        return None, "automation has an unsupported field"
    raw_rules = section.get("rules")
    if raw_rules is None:
        return [], None
    if not isinstance(raw_rules, list):
        return None, "automation.rules must be a list"

    rules = []
    names = set()
    for index, raw_rule in enumerate(raw_rules, 1):
        path = f"automation.rules[{index}]"
        if not isinstance(raw_rule, dict):
            return None, f"{path} must be an object"
        if set(raw_rule) - RULE_FIELDS:
            return None, f"{path} has an unsupported field"

        name = raw_rule.get("name")
        if not isinstance(name, str) or not name.strip():
            return None, f"{path}.name must be a non-empty string"
        name = name.strip()
        if "\t" in name or "\n" in name:
            return None, f"{path}.name must be a single-line string"
        if name in names:
            return None, f"automation.rules contains duplicate name {name!r}"
        names.add(name)

        rule_type = raw_rule.get("type")
        if rule_type not in RULE_TYPES:
            return None, f"{path}.type must be 'session' or 'nudge'"

        message = raw_rule.get("message", "")
        if not isinstance(message, str):
            return None, f"{path}.message must be a string"

        cooldown = raw_rule.get("cooldownMinutes")
        if cooldown is not None and (
            isinstance(cooldown, bool)
            or not isinstance(cooldown, int)
            or cooldown < 0
        ):
            return None, f"{path}.cooldownMinutes must be a non-negative integer"

        normalized_match, error = normalize_match(
            raw_rule.get("match"), f"{path}.match",
        )
        if error:
            return None, error

        rules.append({
            "name": name,
            "type": rule_type,
            "message": message,
            "cooldownMinutes": cooldown,
            "match": normalized_match,
        })
    return rules, None


def _iso(timestamp):
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat()


def _parse_time(value):
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _read_state(state_path):
    try:
        with open(state_path) as f:
            raw = json.load(f)
    except Exception:
        return {"fired": {}, "nudges": {}}
    if not isinstance(raw, dict):
        return {"fired": {}, "nudges": {}}
    fired = raw.get("fired")
    nudges = raw.get("nudges")
    return {
        "fired": {
            key: value for key, value in (fired.items() if isinstance(fired, dict) else [])
            if isinstance(key, str) and isinstance(value, str)
        },
        "nudges": {
            key: value for key, value in (nudges.items() if isinstance(nudges, dict) else [])
            if isinstance(key, str) and isinstance(value, dict)
        },
    }


def _write_state(state_path, state):
    state_path = Path(state_path)
    descriptor, temp_path = tempfile.mkstemp(
        dir=state_path.parent, prefix=f".{state_path.name}.", suffix=".tmp",
    )
    try:
        with os.fdopen(descriptor, "w") as f:
            json.dump(state, f, sort_keys=True)
        os.replace(temp_path, state_path)
    finally:
        if os.path.exists(temp_path):
            os.unlink(temp_path)


def _mutate_state(state_path, mutate):
    """Locked read-modify-write, so two `attention` processes never fire the
    same rule for the same item or drop each other's pending nudges.
    """
    state_path = Path(state_path)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = state_path.with_suffix(".lock")
    with open(lock_path, "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        state = _read_state(state_path)
        result = mutate(state)
        _write_state(state_path, state)
    return result


def _lookup(record, path):
    value = record
    for part in path.split("."):
        if not isinstance(value, dict):
            return None
        value = value.get(part)
    return value


def resolve_message(message, item):
    """Resolve `{field}` templates in a rule's message against one item."""
    def _sub(match):
        name = match.group(1)
        if any(name == reserved or name.startswith(reserved + ".")
               for reserved in _RESERVED_PLACEHOLDERS):
            return match.group(0)
        value = _lookup(item, name)
        return "" if value is None else str(value)

    return _PLACEHOLDER.sub(_sub, message)


def _message_for(rule, item):
    template = rule["message"]
    if not template:
        if rule["type"] == "session":
            template = item.get("session_prompt") or ""
        template = template or "{title}"
    return resolve_message(template, item)


def _due(state, key, rule, now):
    """Whether a rule that already fired for this item may fire again."""
    fired_at = _parse_time(state["fired"].get(key))
    if fired_at is None:
        return True
    cooldown = rule["cooldownMinutes"]
    if cooldown is None:
        return False
    return now - fired_at >= cooldown * 60


def _nudge_item(entry):
    return {
        "_plugin": NUDGE_PLUGIN,
        "_nudge": True,
        "status": NUDGE_STATUS,
        "context": entry.get("context", ""),
        "repo": entry.get("repo", ""),
        "title": entry.get("title", ""),
        "details": entry.get("message", ""),
        "weight": entry.get("weight", 0),
        "id": "",
        "kind": "nudge",
        "actions": [
            {
                "key": "d",
                "label": "dismiss",
                # Core dispatches a nudge action itself, without a plugin.
                "_plugin": NUDGE_PLUGIN,
                "payload": {"kind": "dismiss_nudge", "nudge_id": entry.get("id", "")},
            },
        ],
    }


def load_max_wip(config):
    """The `automation.maxWip` cap on sessions running at once: (limit, error).

    `limit` is None when no cap is configured. Zero is a real cap -- no
    session starts automatically -- which makes it a kill switch for rules
    that are otherwise kept in the configuration.
    """
    section = config.get("automation") if isinstance(config, dict) else None
    if not isinstance(section, dict):
        return None, None
    raw = section.get("maxWip")
    if raw is None:
        return None, None
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
        return None, "automation.maxWip must be a non-negative integer"
    return raw, None


def evaluate(rules, items, *, matches, item_key, start_session, state_path, clock=None):
    """Fire every rule that is due, then return the still-pending nudges.

    A session rule is only recorded as fired once `start_session` reports
    it started, so a failed dispatch is retried on the next refresh.
    Nudges for rules that no longer exist are dropped.
    """
    if not rules:
        return []
    now = (clock or time.time)()
    candidates = [
        item for item in items
        if not item.get("_collector_failure")
        and item.get("_plugin") != NUDGE_PLUGIN
        and not item.get("_nudge")
    ]
    pending = []

    def mutate(state):
        configured = {rule["name"] for rule in rules}
        state["nudges"] = {
            key: entry for key, entry in state["nudges"].items()
            if entry.get("rule") in configured
        }
        for rule in rules:
            for item in candidates:
                key = f"{rule['name']}|{item_key(item)}"
                if not _due(state, key, rule, now):
                    continue
                message = _message_for(rule, item)
                if rule["type"] == "session":
                    if not start_session(item, message):
                        continue
                else:
                    state["nudges"][key] = {
                        "id": key,
                        "rule": rule["name"],
                        "item_key": item_key(item),
                        "title": item.get("title", ""),
                        "context": item.get("context", ""),
                        "repo": item.get("repo", ""),
                        "message": message,
                        "weight": max(item.get("weight", 0) or 0, 0) + 5,
                        "created": _iso(now),
                    }
                state["fired"][key] = _iso(now)
        pending.extend(
            _nudge_item(entry)
            for entry in state["nudges"].values()
            if entry.get("rule") in configured
        )
        return True

    _mutate_state(state_path, mutate)
    return pending


def dismiss_nudge(nudge_id, state_path):
    """Drop one pending nudge. Returns whether it was still pending."""
    if not isinstance(nudge_id, str) or not nudge_id:
        return False

    def mutate(state):
        return state["nudges"].pop(nudge_id, None) is not None

    return _mutate_state(state_path, mutate)
