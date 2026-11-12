"""dashboard -- the interactive dashboard's progressive-fetch controller.

Standalone module: no import of `attention` anywhere in this file.
`attention`, being extensionless, can't be imported as an ordinary module.
Instead, `attention.run_dashboard()` constructs a `DashboardController` and
hands it every core operation it needs as a plain callable:

- `fetch_plugin(name) -> list[dict]` -- runs one plugin's `fetch()`,
  isolated so one plugin's exception never takes down the round.
- `build_snapshot(items_by_plugin, recently_acted) -> list[dict]` -- the
  flatten/cross-link/age-boost/recently-acted/sort pipeline, extracted
  from `attention.build_prioritized_items`.
- `render_rows(items) -> list[str]` -- the dashboard-only row renderer
  (`attention.render_dashboard_rows`), producing the 4-field row text a
  `Presenter` actually displays. Kept as its own injected callable
  (distinct from `build_snapshot`, whose typed return is items, not
  strings) so this module never needs to know `render_dashboard_rows`
  exists, let alone import it.
- `act(key, row) -> None` -- `attention.act`, unchanged.
- `acknowledge_action() -> None` -- lets a user read an action's terminal
  output before the presenter redraws.

This keeps the dependency direction one-way (`attention` -> `dashboard`,
never back) and lets tests build a `DashboardController` entirely from
fakes -- no real plugin loading, no real CLI process, and no on-disk config.
"""
from __future__ import annotations

import base64
import json
import threading
import time
from typing import Callable, NamedTuple, Protocol


class PresenterResult(NamedTuple):
    key: str | None
    row: str


class Presenter(Protocol):
    def launch(self) -> None: ...
    def push_snapshot(self, rows: list[str], pending: list[str]) -> None: ...
    def wait_for_exit(self) -> PresenterResult: ...
    def stop(self) -> None: ...


_IDLE_HEADER = "Hotkeys act immediately · r = refresh · Enter = primary action · Esc = quit"


def _pending_header(pending):
    import shutil
    w = shutil.get_terminal_size().columns
    if w < 80:
        if w < 32:
            idle = "Enter/Esc"
        elif w < 50:
            idle = "Keys act immediately · Enter/Esc"
        elif w < 65:
            idle = "Keys act immediately · Enter=primary · Esc=quit"
        else:
            idle = "Hotkeys act immediately · Enter = primary · Esc = quit"
    else:
        idle = _IDLE_HEADER

    if pending:
        msg = "Loading: " + ", ".join(pending) + "…"
        if w < 80 and len(msg) > w and w >= 15:
            if w < 40:
                return "Loading…"
            else:
                allowed_len = w - 10
                plugins_str = ", ".join(pending)
                if len(plugins_str) > allowed_len:
                    plugins_str = plugins_str[:allowed_len - 3] + "..."
                return f"Loading: {plugins_str}…"
        return msg
    return idle


class _Round:
    def __init__(self, plugin_names, generation, items_by_plugin, pending):
        self.generation = generation
        self.pending = pending
        self.items_by_plugin = items_by_plugin
        self.terminal = not pending


class DashboardController:
    """Fetches every configured plugin concurrently and republishes a
    fresh, fully recomputed snapshot to one long-lived `Presenter` each
    time a plugin completes -- enforcing, unconditionally, that at most
    one fetch round is ever in flight (see design.md Decision 6).
    """

    def __init__(
        self, plugin_names, presenter, *,
        fetch_plugin: Callable[[str], list],
        build_snapshot: Callable[[dict, dict], list],
        render_rows: Callable[[list], list],
        act: Callable[[str, str], object],
        acknowledge_action: Callable[[], None] = lambda: None,
        on_fetch_error: Callable[[str, Exception], dict] | None = None,
        cache_ttl: float = 60,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.plugin_names = list(plugin_names)
        self.presenter = presenter
        self._fetch_plugin = fetch_plugin
        self._build_snapshot = build_snapshot
        self._render_rows = render_rows
        self._act = act
        self._acknowledge_action = acknowledge_action
        self._cache_ttl = cache_ttl
        self._clock = clock
        self._on_fetch_error = on_fetch_error or self._default_fetch_error
        self._cache = {}
        self._state_lock = threading.Lock()
        self._presenter_lock = threading.Lock()
        self._generation = 0
        self._round = None
        self._recently_acted = {}
        self._empty_final = False
        self._closed = False
        self._refresh_after_round = False

    @staticmethod
    def _default_fetch_error(name, exc):
        return {
            "_collector_failure": True, "_plugin": name,
            "status": "FETCH FAILED", "title": f"{name} collector failed",
            "context": name, "details": type(exc).__name__,
            "weight": 0, "actions": [],
        }

    def run(self):
        """Fetch once on open; subsequent rounds start only on explicit refresh."""
        if not self.plugin_names:
            return False
        try:
            self._begin_round()
            while True:
                result = self.presenter.wait_for_exit()
                if result.key == "r":
                    self._refresh()
                    continue
                if result.row == "":
                    return not self._empty_final
                if result.key == "":
                    self._relaunch_presenter()
                    continue
                action = self._action_for(result.key, result.row)
                item_id = action.get("_item_id") if action else None
                completed = self._act(result.key, result.row)
                if completed is not False and action:
                    self._invalidate_plugin(action.get("_plugin"))
                self._acknowledge_action()
                if action and not action.get("read"):
                    # Read-only actions (open, yank) leave the item in place
                    # so it can be acted on again straight away.
                    self._deprioritize(item_id)
                self._relaunch_presenter()
        finally:
            with self._presenter_lock:
                with self._state_lock:
                    self._closed = True
                self.presenter.stop()

    def _refresh(self):
        if self._round_is_terminal():
            self._begin_round(force=True)
        else:
            with self._state_lock:
                self._refresh_after_round = True
            self._relaunch_presenter()

    def _next_generation(self):
        self._generation += 1
        return self._generation

    def _round_is_terminal(self):
        with self._state_lock:
            return self._round is None or self._round.terminal

    def _begin_round(self, force=False):
        round_ = self._create_round(force=force)
        self._relaunch_presenter()
        self._submit_round_fetches(round_)

    def _create_round(self, force=False):
        now = self._clock()
        with self._state_lock:
            cached = {
                name: entry for name in self.plugin_names
                if (entry := self._cache.get(name)) is not None
            }
            items_by_plugin = {name: entry[0] for name, entry in cached.items()}
            pending = set(self.plugin_names) if force else {
                name for name in self.plugin_names
                if name not in cached or cached[name][1] <= now
            }
            round_ = _Round(
                self.plugin_names, self._next_generation(), items_by_plugin, pending,
            )
            self._round = round_
            self._refresh_after_round = False
        return round_

    def _submit_round_fetches(self, round_):
        for name in self.plugin_names:
            if name not in round_.pending:
                continue
            threading.Thread(
                target=self._run_plugin_fetch, args=(round_, name), daemon=True,
            ).start()

    def _run_plugin_fetch(self, round_, name):
        """Fetch one plugin without letting failures stop other collectors."""
        try:
            items = self._fetch_plugin(name)
        except Exception as exc:
            items = [self._on_fetch_error(name, exc)]
            self._on_plugin_result(round_, name, items, False)
            return
        self._on_plugin_result(round_, name, items, True)

    def _on_plugin_result(self, round_, name, items, succeeded):
        with self._state_lock:
            if self._closed or self._round is not round_:
                return
            if succeeded:
                self._cache[name] = (items, self._clock() + self._cache_ttl)
            round_.items_by_plugin[name] = items
            round_.pending.discard(name)
            terminal_now = not round_.pending
            if terminal_now:
                round_.terminal = True
            refresh_after_round = terminal_now and self._refresh_after_round
            items_by_plugin = dict(round_.items_by_plugin)
            pending = [n for n in self.plugin_names if n in round_.pending]
        if refresh_after_round:
            self._refresh()
            return
        rows = self._render_snapshot(items_by_plugin)
        with self._presenter_lock:
            with self._state_lock:
                if self._closed:
                    return
            if terminal_now and not rows:
                self._empty_final = True
                self.presenter.stop()
                return
            self.presenter.push_snapshot(rows, pending)
    def _relaunch_presenter(self):
        with self._state_lock:
            round_ = self._round
            items_by_plugin = dict(round_.items_by_plugin)
            pending = [n for n in self.plugin_names if n in round_.pending]
            terminal = round_.terminal
        rows = self._render_snapshot(items_by_plugin)
        with self._presenter_lock:
            if self._empty_final:
                return
            if terminal and not rows:
                self._empty_final = True
                self.presenter.stop()
                return
            self.presenter.launch()
            self.presenter.push_snapshot(rows, pending)

    def _render_snapshot(self, items_by_plugin):
        with self._state_lock:
            recently_acted = dict(self._recently_acted)
        items = self._build_snapshot(items_by_plugin, recently_acted)
        return self._render_rows(items)

    def _action_for(self, key, row):
        parts = row.split("\t", 1)
        if len(parts) < 2:
            return None
        try:
            actions = json.loads(base64.b64decode(parts[1].split("\t", 1)[0]).decode())
        except Exception:
            return None
        for action in actions:
            if action.get("key") == key or (key == "" and action.get("primary")):
                return action
        return None

    def _item_id_for(self, key, row):
        action = self._action_for(key, row)
        return action.get("_item_id") if action else None

    def _invalidate_plugin(self, name):
        if not name:
            return
        with self._state_lock:
            self._cache.pop(name, None)

    def _deprioritize(self, item_id):
        if not item_id:
            return
        with self._state_lock:
            self._recently_acted.pop(item_id, None)
            self._recently_acted[item_id] = True
            if len(self._recently_acted) > 10:
                del self._recently_acted[next(iter(self._recently_acted))]


def _read_key(screen):
    """Decode arrow sequences not recognized by the active terminfo entry."""
    import curses
    key = screen.getch()

    if key != 27:
        return key
    try:
        if curses.isendwin():
            return key
    except curses.error:
        return key
    sequence = []
    first = screen.getch()
    sequence.append(first)
    if first in (ord("["), ord("O")):
        second = screen.getch()
        sequence.append(second)
        if second == ord("A"):
            return curses.KEY_UP
        if second == ord("B"):
            return curses.KEY_DOWN
    for next_key in reversed(sequence):
        if next_key != -1:
            curses.ungetch(next_key)
    return 27


def _set_escape_delay(curses):
    try:
        curses.set_escdelay(100)
    except (AttributeError, curses.error):
        pass


class CursesPresenter:
    """Production presenter backed entirely by the terminal's curses UI."""

    def __init__(self, title="Attention"):
        self._title = title
        self._lock = threading.Lock()
        self._rows = []
        self._pending = []
        self._stopped = False
        self._snapshot_version = 0
        self._last_selection = 0
        self._expanded_action_rows = set()

    def launch(self):
        with self._lock:
            self._stopped = False

    def push_snapshot(self, rows, pending):
        with self._lock:
            self._rows = list(rows)
            self._pending = list(pending)
            self._snapshot_version += 1

    def wait_for_exit(self):
        import curses

        return curses.wrapper(self._run)

    def stop(self):
        with self._lock:
            self._stopped = True

    def _snapshot(self):
        with self._lock:
            return list(self._rows), list(self._pending), self._stopped, self._snapshot_version

    @staticmethod
    def _matching_rows(rows, query):
        terms = query.casefold().split()
        if not terms:
            return rows
        return [
            row for row in rows
            if all(term in row.split("\t", 1)[0].casefold() for term in terms)
        ]

    @staticmethod
    def _action_keys(row):
        fields = row.split("\t")
        return fields[2].split(",") if len(fields) > 2 and fields[2] else []

    @staticmethod
    def _hint_lines(row):
        fields = row.split("\t")
        return fields[3].split("\x0b") if len(fields) > 3 and fields[3] else []

    def _display_hints(self, row, width):
        import textwrap

        try:
            actions = json.loads(base64.b64decode(row.split("\t")[1]))
            linked = [action for action in actions if "_original_key" in action]
            if not linked:
                return self._hint_lines(row)
            identity = row.split("\t")[1]
            expanded = identity in self._expanded_action_rows
            visible = actions if expanded else [action for action in actions if "_original_key" not in action]
            parts = [f"{action['key']} {action['label']}" for action in visible]
            parts.append(f"? {'hide' if expanded else 'show'} linked actions ({len(linked)})")
            return textwrap.wrap("  ".join(parts), width=max(1, width - 1),
                                 replace_whitespace=False, drop_whitespace=True)
        except (IndexError, TypeError, ValueError, KeyError):
            return self._hint_lines(row)

    def _toggle_linked_actions(self, row):
        try:
            identity = row.split("\t")[1]
            actions = json.loads(base64.b64decode(identity))
            if not any("_original_key" in action for action in actions):
                return False
            if identity in self._expanded_action_rows:
                self._expanded_action_rows.remove(identity)
            else:
                self._expanded_action_rows.add(identity)
            return True
        except (IndexError, TypeError, ValueError):
            return False

    @staticmethod
    def _indicator_header(rows):
        if not rows:
            return ""
        fields = rows[0].split("\t")
        if len(fields) < 5:
            return ""
        try:
            metadata = json.loads(fields[4])
            offset = metadata["offset"]
            columns = metadata["columns"]
            if not isinstance(offset, int) or offset < 0 or not isinstance(columns, list):
                return ""
            return " " * offset + "  ".join(
                f"{label:<{width}}"
                for label, width in columns
                if isinstance(label, str) and isinstance(width, int) and width > 0
            )
        except (TypeError, ValueError, KeyError):
            return ""

    def _draw(self, screen, rows, pending, selected, query):
        import curses

        height, width = screen.getmaxyx()
        screen.erase()

        def write(line, text, attr=0):
            if line < height and width > 1:
                try:
                    screen.addnstr(line, 0, text, width - 1, attr)
                except curses.error:
                    pass

        visible = self._matching_rows(rows, query or "")
        if visible:
            selected %= len(visible)
        else:
            selected = 0
        hint_lines = self._display_hints(visible[selected], width) if visible else []
        footer_start = max(3, height - max(1, len(hint_lines)))
        capacity = max(0, footer_start - 3)
        start = min(max(0, selected - capacity + 1), max(0, len(visible) - capacity))

        write(0, self._title, curses.A_BOLD)
        write(1, _pending_header(pending))
        header = self._indicator_header(rows)
        filter_text = f"Filter: {query}" if query is not None else ""
        header_line = f"{filter_text}  {header}" if filter_text and header else header or filter_text
        write(2, header_line)
        if not visible:
            write(3, "No matching items.")
        for index, row in enumerate(visible[start:start + capacity], start):
            attr = curses.A_REVERSE if index == selected else 0
            write(3 + index - start, row.split("\t", 1)[0], attr)
        for index, hint in enumerate(hint_lines):
            write(footer_start + index, hint)
        screen.refresh()
        return visible, selected

    def _read_key(self, screen):
        return _read_key(screen)

    def _run(self, screen):
        import curses

        _set_escape_delay(curses)
        screen.keypad(True)
        screen.timeout(100)
        try:
            curses.curs_set(0)
        except curses.error:
            pass
        selected = self._last_selection
        query = None
        while True:
            rows, pending, stopped, version = self._snapshot()
            if stopped:
                return PresenterResult("", "")
            visible, selected = self._draw(screen, rows, pending, selected, query)
            key = self._read_key(screen)
            if query is None and key == ord("r"):
                return PresenterResult("r", "")
            if key == -1:
                # Check for a new snapshot pushed by the controller
                # (e.g. after an action) without exiting the loop.
                _, _, stopped2, version2 = self._snapshot()
                if stopped2 or version2 != version:
                    continue
                continue
            if key == 27:
                return PresenterResult("", "")
            if key in (curses.KEY_BACKSPACE, 127, 8):
                if query:
                    query = query[:-1]
                elif query is not None:
                    query = None
                continue
            if key == ord("q"):
                return PresenterResult("", "")
            if key in (curses.KEY_UP, ord("k")) and visible:
                selected = (selected - 1) % len(visible)
                continue
            if key in (curses.KEY_DOWN, ord("j")) and visible:
                selected = (selected + 1) % len(visible)
                continue
            if query is None and key == ord("?") and visible:
                self._toggle_linked_actions(visible[selected])
                continue
            if query is None and visible and 0 <= key <= 255:
                char = chr(key)
                if char not in "qjk" and char in self._action_keys(visible[selected]):
                    self._last_selection = selected
                    return PresenterResult(char, visible[selected])
            if key in (curses.KEY_ENTER, 10, 13) and visible:
                self._last_selection = selected
                return PresenterResult("", visible[selected])
            if key == ord("/"):
                query = ""
                continue
            if query is not None and 0 <= key <= 255:
                char = chr(key)
                if char.isprintable() and not ("A" <= char <= "Z") and char not in "jk":
                    query += char


class CursesGroupPresenter(CursesPresenter):
    """A single navigable tree of expandable dashboard groups."""

    def __init__(self, group_names, expanded=()):
        super().__init__()
        self._group_names = list(group_names)
        self._expanded = {
            name for name in expanded if name in self._group_names
        }

    def _entries(self, rows, query):
        grouped = {name: [] for name in self._group_names}
        for row in rows:
            item, _, group = row.rpartition("\t")
            if group in grouped:
                grouped[group].append(item)
        entries = []
        for group in self._group_names:
            items = grouped[group]
            matching = self._matching_rows(items, query or "")
            if not items or (query and not matching):
                continue
            entries.append((group, None, len(items)))
            if group in self._expanded or query:
                entries.extend((group, item, len(items)) for item in matching)
        return entries

    def _draw_tree(self, screen, entries, pending, selected, query):
        import curses

        height, width = screen.getmaxyx()
        screen.erase()
        if entries:
            selected = min(selected, len(entries) - 1)
        else:
            selected = 0
        item = entries[selected][1] if entries else None
        hints = self._display_hints(item, width) if item else ["Enter expand/collapse · / filter · r refresh · Esc quit"]
        footer_start = max(3, height - max(1, len(hints)))
        capacity = max(0, footer_start - 3)
        start = min(max(0, selected - capacity + 1), max(0, len(entries) - capacity))

        def write(line, text, attr=0):
            if line < height and width > 1:
                try:
                    screen.addnstr(line, 0, text, width - 1, attr)
                except curses.error:
                    pass

        write(0, "Attention", curses.A_BOLD)
        write(1, _pending_header(pending))
        if query is not None:
            write(2, f"Filter: {query}")
        elif item:
            write(2, "  " + self._indicator_header([item]))
        if not entries:
            write(3, "No matching items.")
        for index, (group, row, count) in enumerate(entries[start:start + capacity], start):
            if row is None:
                marker = "▾" if group in self._expanded or query else "▸"
                text = f"{marker} {group} ({count})"
            else:
                text = "  " + row.split("\t", 1)[0]
            write(3 + index - start, text, curses.A_REVERSE if index == selected else 0)
        for index, hint in enumerate(hints):
            write(footer_start + index, hint)
        screen.refresh()
        return selected

    def _run(self, screen):
        import curses

        _set_escape_delay(curses)
        screen.keypad(True)
        screen.timeout(100)
        try:
            curses.curs_set(0)
        except curses.error:
            pass
        selected = self._last_selection
        query = None
        while True:
            rows, pending, stopped, _ = self._snapshot()
            if stopped:
                return PresenterResult("", "")
            entries = self._entries(rows, query)
            selected = self._draw_tree(screen, entries, pending, selected, query)
            key = self._read_key(screen)
            if key == -1:
                continue
            if key == 27 or key == ord("q"):
                return PresenterResult("", "")
            if key == ord("r") and query is None:
                self._last_selection = selected
                return PresenterResult("r", "")
            if key in (curses.KEY_BACKSPACE, 127, 8):
                if query:
                    query = query[:-1]
                elif query is not None:
                    query = None
                continue
            if key in (curses.KEY_UP, ord("k")) and entries:
                selected = (selected - 1) % len(entries)
                continue
            if key in (curses.KEY_DOWN, ord("j")) and entries:
                selected = (selected + 1) % len(entries)
                continue
            if key in (curses.KEY_ENTER, 10, 13) and entries:
                group, row, _ = entries[selected]
                if row is None:
                    if group in self._expanded:
                        self._expanded.remove(group)
                    else:
                        self._expanded.add(group)
                else:
                    self._last_selection = selected
                    return PresenterResult("", row)
                continue
            if query is None and key == ord("?") and entries and entries[selected][1]:
                self._toggle_linked_actions(entries[selected][1])
                continue
            if query is None and entries and entries[selected][1] and 0 <= key <= 255:
                char = chr(key)
                row = entries[selected][1]
                if char not in "qjk" and char in self._action_keys(row):
                    self._last_selection = selected
                    return PresenterResult(char, row)
            if key == ord("/"):
                query = ""
                continue
            if query is not None and 0 <= key <= 255:
                char = chr(key)
                if char.isprintable() and not ("A" <= char <= "Z") and char not in "jk":
                    query += char
