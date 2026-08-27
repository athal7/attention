"""generic plugin -- define an arbitrary data source purely from config:
a command that prints a JSON array, plus templates mapping each record's
fields onto the standard item shape and each action's argv. No Python
file needed for a source that's just "run a command, map its JSON
output" -- write a provider config instead of a plugin.

Config (config["generic"]): a dict of named providers, e.g.

    {
      "plugins": ["github", "generic"],
      "generic": {
        "jules-prs": {
          "command": ["gh", "search", "prs", "--author=google-labs-jules[bot]",
                       "--state=open", "--json", "number,title,repository,url"],
          "status": "NEEDS REVIEW",
          "context": "{repository.nameWithOwner}",
          "title": "{title}",
          "id": "{number}",
          "weight": 85,
          "actions": [
            {"key": "O", "label": "open", "primary": true, "command": ["open", "{url}"]}
          ]
        }
      }
    }

Every text field (status/context/title/details/id, and each action's
command tokens) is a template: plain text is used as-is; `{dotted.path}`
substitutes that field from the matching JSON record (missing paths
become ""). `weight` is a plain int, or a `{path}` template resolved
then parsed as one (falling back to 50 if that fails).

Each provider is fetched and mapped independently -- one bad provider
(missing/failing command, non-JSON-array output) contributes nothing,
without affecting the others.
"""
import concurrent.futures
import json
import re
import subprocess

from _util import resolve_configured_actions, run_configured_action

_PLACEHOLDER = re.compile(r"\{([\w.]+)\}")




def _lookup(record, path):
    val = record
    for part in path.split("."):
        if not isinstance(val, dict):
            return None
        val = val.get(part)
    return val


def _resolve(template, record):
    if not isinstance(template, str):
        return template
    def _sub(m):
        val = _lookup(record, m.group(1))
        return "" if val is None else str(val)
    return _PLACEHOLDER.sub(_sub, template)


def _resolve_weight(spec, record, default=50):
    if isinstance(spec, bool):
        return default
    if isinstance(spec, int):
        return spec
    if spec is None:
        return default
    try:
        return int(_resolve(spec, record))
    except (TypeError, ValueError):
        return default


def _fetch_provider(name, spec):
    command = spec.get("command")
    if not command:
        return []
    try:
        res = subprocess.run(
            command, capture_output=True, text=True,
            stdin=subprocess.DEVNULL, timeout=30,
        )
        if res.returncode != 0:
            return []
        records = json.loads(res.stdout or "[]")
    except Exception:
        return []
    if not isinstance(records, list):
        return []

    items = []
    for record in records:
        if not isinstance(record, dict):
            continue
        items.append({
            "status": _resolve(spec.get("status", name), record),
            "context": _resolve(spec.get("context", name), record),
            "title": _resolve(spec.get("title", ""), record),
            "details": _resolve(spec.get("details", ""), record),
            "weight": _resolve_weight(spec.get("weight"), record),
            "id": _resolve(spec.get("id", ""), record),
            "actions": resolve_configured_actions(spec.get("actions", []), record),
        })
    return items


def fetch(config):
    providers = {
        name: spec for name, spec in config.get("generic", {}).items()
        if isinstance(spec, dict)
    }
    if not providers:
        return []
    # Each provider's command is an independent subprocess (often a
    # network-bound CLI call, per the module docstring's `gh search prs`
    # example) -- fetched concurrently so N slow providers cost as long
    # as the slowest one, not their sum. Capped at a fixed ceiling,
    # independent of how many providers are configured, so a large
    # `generic` config can't spawn one process per provider at once.
    items = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(len(providers), 8)) as pool:
        futures = [pool.submit(_fetch_provider, name, spec) for name, spec in providers.items()]
        for fut in futures:
            items.extend(fut.result())
    return items


def act(key, payload):
    return run_configured_action(payload)
