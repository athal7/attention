"""reminders plugin -- open reminders on configured lists, via
`remindctl` (https://github.com/steipete/remindctl).

Config (config["reminders"]):
    {"lists": ["Personal", "Work"]}   # list names to pull open items from
"""
import json
import shlex
import subprocess
from datetime import date

from _util import run_cmd


def fetch(config):
    list_names = config.get("reminders", {}).get("lists", [])
    if not list_names:
        return []

    custom_cmd = config.get("reminders", {}).get("command")
    if custom_cmd:
        cmd_prefix = shlex.split(custom_cmd)
    else:
        cmd_prefix = ["remindctl"]

    try:
        res = subprocess.run(cmd_prefix + ["show", "all", "--json"], capture_output=True, text=True)
        if res.returncode != 0:
            return []
        raw = json.loads(res.stdout or "[]")
    except Exception:
        return []

    items = []
    for r in raw:
        if r.get("isCompleted") or r.get("listName") not in list_names:
            continue
        title = r.get("title", "Untitled").strip().replace("\t", " ").replace("|", "/")
        list_name = r.get("listName", "").replace("\t", " ").replace("|", "/")
        prio = r.get("priority", "none").lower()
        due = r.get("dueDate", "")
        reminder_id = r.get("id", "")

        weight = 50
        status = "PENDING"
        details = ""
        if due:
            details = f"Due: {due[:10]}"
            status = "DUE"
            try:
                due_date = date.fromisoformat(due[:10])
                if due_date < date.today():
                    weight, status = 95, "OVERDUE"
                elif due_date == date.today():
                    weight, status = 85, "DUE TODAY"
            except Exception:
                pass
        else:
            weight = {"high": 70, "medium": 50, "low": 30}.get(prio, 15)

        items.append({
            "status": status,
            "context": list_name,
            "title": title,
            "details": details,
            "weight": weight,
            "id": reminder_id,
            "absorb_note": f"Reminder: {title}",
            "actions": [
                {"key": "alt-x", "label": "complete", "payload": {"id": reminder_id, "command": cmd_prefix}},
            ],
        })
    return items


def act(key, payload):
    if key == "alt-x":
        cmd_prefix = payload.get("command") or ["remindctl"]
        run_cmd(cmd_prefix + ["complete", payload["id"]])
