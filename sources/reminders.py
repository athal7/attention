"""reminders plugin -- open reminders on configured lists, via
`remindctl` (https://github.com/steipete/remindctl).

Config (config["reminders"]):
    {"lists": ["Personal", "Work"]}   # list names to pull open items from
"""
import json
import subprocess
from datetime import date

from _util import resolve_configured_actions, run_cmd, run_configured_action



def fetch(config):
    list_names = config.get("reminders", {}).get("lists", [])
    if not list_names:
        return []

    try:
        res = subprocess.run(["remindctl", "show", "all", "--json"], capture_output=True, text=True)
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

        record = {
            "id": reminder_id,
            "title": title,
            "context": list_name,
            "status": status,
            "details": details,
        }
        actions = [
            {"key": "X", "label": "complete", "payload": {"id": reminder_id}},
        ]
        configured_actions = config.get("reminders", {}).get("actions", [])
        actions.extend(resolve_configured_actions(configured_actions, record))

        items.append({
            "status": status,
            "context": list_name,
            "title": title,
            "details": details,
            "weight": weight,
            "id": reminder_id,
            "absorb_note": f"Reminder: {title}",
            "actions": actions,
        })
    return items


def act(key, payload):
    if "command" in payload:
        run_configured_action(payload)
        return
    if key == "X":
        run_cmd(["remindctl", "complete", payload["id"]])
