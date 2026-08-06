"""calendar plugin -- near-term events on configured calendars, via the
`ical` CLI (https://github.com/BRO3886/ical).

Config (config["calendar"]):
    {"names": ["Work"]}   # calendar names to pull events from; [] = none
"""
import json
import subprocess
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from _util import copy_to_clipboard


def _get_ical_path():
    # Prefer a fixed known-good path over PATH resolution: a stray dev
    # build shadowing `ical` on PATH (e.g. a mise shim) is ad-hoc-signed
    # separately from wherever your managed release binary lives, so
    # macOS TCC treats them as different app identities and Calendar
    # access doesn't carry over.
    local_path = Path.home() / ".local/bin/ical"
    if local_path.exists():
        return str(local_path)
    try:
        which = subprocess.run(["which", "ical"], capture_output=True, text=True)
        if which.returncode == 0 and which.stdout.strip():
            return which.stdout.strip()
    except Exception:
        pass
    return "ical"


def fetch(config):
    cal_names = config.get("calendar", {}).get("names", [])
    if not cal_names:
        return []

    ical_bin = _get_ical_path()
    today_str = str(date.today())
    tomorrow_str = str(date.today() + timedelta(days=1))

    events = []
    for cal in cal_names:
        try:
            cmd = [ical_bin, "list", "-c", cal, "--from", today_str, "--to", tomorrow_str, "-o", "json"]
            res = subprocess.run(cmd, capture_output=True, text=True)
            if res.returncode == 0 and res.stdout.strip():
                for e in json.loads(res.stdout):
                    if (
                        e.get("status") == "cancelled"
                        or e.get("availability") == "free"
                        or e.get("self_status") == "declined"
                    ):
                        continue
                    e["_calendar_name"] = cal
                    events.append(e)
        except Exception:
            continue

    items = []
    for e in events:
        title = e.get("title", "Untitled").strip().replace("\t", " ").replace("|", "/")
        all_day = e.get("all_day", False)
        start_str = e.get("start_date", "")
        calendar_name = e.get("_calendar_name", "")

        weight = 50
        status = "ALL DAY"
        time_tag = ""
        if not all_day and start_str:
            try:
                dt = datetime.fromisoformat(start_str.replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                now = datetime.now(timezone.utc)
                diff_mins = (dt - now).total_seconds() / 60.0
                # Local timezone, 12-hour clock (e.g. "2:00 PM") rather
                # than the event's origin offset or a 24-hour clock.
                time_tag = dt.astimezone().strftime("%I:%M %p").lstrip("0")
                if 0 <= diff_mins <= 30:
                    weight, status = 100, "NOW"
                elif 0 <= diff_mins <= 120:
                    weight, status = 80, "SOON"
                elif diff_mins < 0:
                    weight, status = 20, "PAST"
                else:
                    status = "UPCOMING"
            except Exception:
                time_tag = start_str[:16].replace("T", " ")
                status = "UPCOMING"

        display_text = f"{title} - {time_tag}" if time_tag else title
        items.append({
            "status": status,
            "context": calendar_name,
            "title": title,
            "details": time_tag,
            "weight": weight,
            "id": e.get("id", ""),
            "actions": [
                {"key": "alt-y", "label": "yank", "payload": {"text": display_text}},
            ],
        })
    return items


def act(key, payload):
    if key == "alt-y":
        copy_to_clipboard(payload["text"])
