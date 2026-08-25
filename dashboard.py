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

This keeps the dependency direction one-way (`attention` -> `dashboard`,
never back) and lets tests build a `DashboardController` entirely from
fakes -- no real plugin loading, no real CLI process, and no on-disk config.
"""
import base64
import json
import threading
import time
from typing import Callable, NamedTuple, Protocol


class PresenterResult(NamedTuple):
    key: str | None  # None means wait_for_exit's timeout elapsed
    row: str


class Presenter(Protocol):
    def launch(self) -> None: ...
    def push_snapshot(self, rows: list[str], pending: list[str]) -> None: ...
    def wait_for_exit(self, timeout: float) -> PresenterResult: ...
    def stop(self) -> None: ...


_IDLE_HEADER = "Hotkeys act immediately on this row · Enter = primary action · Esc = quit"


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
    def __init__(self, plugin_names, generation):
        self.generation = generation
        self.pending = set(plugin_names)
        self.items_by_plugin = {}
        self.terminal = False


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
        act: Callable[[str, str], None],
    ):
        self.plugin_names = list(plugin_names)
        self.presenter = presenter
        self._fetch_plugin = fetch_plugin
        self._build_snapshot = build_snapshot
        self._render_rows = render_rows
        self._act = act

        self._state_lock = threading.Lock()
        self._presenter_lock = threading.Lock()
        self._generation = 0
        self._round = None
        self._recently_acted = {}
        self._empty_final = False
        self._closed = False

    def run(self, refresh_interval=60):
        """Runs until the user quits (Esc) or the merged snapshot goes
        permanently empty. Returns True if the session ended because the
        user quit (whether or not any snapshot ever had items), False if
        it ended because every provider finished with nothing to show at
        all -- the caller's cue to print the same "nothing needs
        attention" message used for the zero-configured-plugins case.
        """
        if not self.plugin_names:
            return False
        try:
            self._begin_round()
            while True:
                result = self.presenter.wait_for_exit(refresh_interval)
                if result.key is None:
                    self._advance_round()
                    continue
                if result.row == "":
                    return not self._empty_final
                item_id = self._item_id_for(result.key, result.row)
                self._act(result.key, result.row)
                self._deprioritize(item_id)
                self._advance_round()
        finally:
            with self._presenter_lock:
                with self._state_lock:
                    self._closed = True
                self.presenter.stop()

    def _next_generation(self):
        self._generation += 1
        return self._generation

    def _round_is_terminal(self):
        with self._state_lock:
            return self._round is None or self._round.terminal

    def _advance_round(self):
        """A periodic timeout or a dispatched hotkey both call this: a
        new fetch round starts only if the active round has already
        gone terminal (design.md Decision 6's hard invariant); either
        way the presenter is always relaunched, primed with whatever the
        active round has produced so far.
        """
        if self._round_is_terminal():
            self._begin_round()
        else:
            self._relaunch_presenter()

    def _begin_round(self):
        """Creates a fresh round and relaunches the presenter against
        its (still all-pending) state *before* submitting a single
        fetch_plugin() call -- otherwise a plugin fast enough to finish
        before this method returns could publish its own snapshot ahead
        of the round's own initial launch, showing it out of order.
        """
        round_ = self._create_round()
        self._relaunch_presenter()
        self._submit_round_fetches(round_)

    def _create_round(self):
        with self._state_lock:
            round_ = _Round(self.plugin_names, self._next_generation())
            self._round = round_
        return round_

    def _submit_round_fetches(self, round_):
        for name in self.plugin_names:
            threading.Thread(
                target=self._run_plugin_fetch, args=(round_, name), daemon=True,
            ).start()

    def _run_plugin_fetch(self, round_, name):
        """Runs on its own plain daemon thread -- never a
        ThreadPoolExecutor worker -- so a plugin's fetch_plugin() call
        that never returns can never block interpreter exit.
        ThreadPoolExecutor's workers are ordinary (non-daemon) threads
        the stdlib joins at interpreter shutdown; a daemon thread never
        is, so a permanently hung synchronous plugin call only ever
        leaves its own round non-terminal (design.md Decision 6), never
        the process itself.
        """
        try:
            items = self._fetch_plugin(name)
        except Exception:
            items = []
        self._on_plugin_result(round_, name, items)

    def _on_plugin_result(self, round_, name, items):
        with self._state_lock:
            if self._closed or self._round is not round_:
                return
            round_.items_by_plugin[name] = items
            round_.pending.discard(name)
            terminal_now = not round_.pending
            if terminal_now:
                round_.terminal = True
            items_by_plugin = dict(round_.items_by_plugin)
            pending = [n for n in self.plugin_names if n in round_.pending]

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
        rows = self._render_snapshot(items_by_plugin)
        with self._presenter_lock:
            if self._empty_final:
                return
            self.presenter.launch()
            self.presenter.push_snapshot(rows, pending)

    def _render_snapshot(self, items_by_plugin):
        with self._state_lock:
            recently_acted = dict(self._recently_acted)
        items = self._build_snapshot(items_by_plugin, recently_acted)
        return self._render_rows(items)

    def _item_id_for(self, key, row):
        parts = row.split("\t", 1)
        if len(parts) < 2:
            return None
        try:
            actions = json.loads(base64.b64decode(parts[1].split("\t", 1)[0]).decode())
        except Exception:
            return None
        for a in actions:
            if a.get("key") == key or (key == "" and a.get("primary")):
                return a.get("_item_id")
        return None

    def _deprioritize(self, item_id):
        if not item_id:
            return
        with self._state_lock:
            self._recently_acted.pop(item_id, None)
            self._recently_acted[item_id] = True
            if len(self._recently_acted) > 10:
                del self._recently_acted[next(iter(self._recently_acted))]


class CursesPresenter:
    """Production presenter backed entirely by the terminal's curses UI."""

    def __init__(self, title="Attention"):
        self._title = title
        self._lock = threading.Lock()
        self._rows = []
        self._pending = []
        self._stopped = False

    def launch(self):
        with self._lock:
            self._stopped = False

    def push_snapshot(self, rows, pending):
        with self._lock:
            self._rows = list(rows)
            self._pending = list(pending)

    def wait_for_exit(self, timeout):
        import curses

        return curses.wrapper(self._run, time.monotonic() + timeout)

    def stop(self):
        with self._lock:
            self._stopped = True

    def _snapshot(self):
        with self._lock:
            return list(self._rows), list(self._pending), self._stopped

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

        visible = self._matching_rows(rows, query)
        if visible:
            selected %= len(visible)
        else:
            selected = 0
        hint_lines = self._hint_lines(visible[selected]) if visible else []
        footer_start = max(3, height - max(1, len(hint_lines)))
        capacity = max(0, footer_start - 3)
        start = min(max(0, selected - capacity + 1), max(0, len(visible) - capacity))

        write(0, self._title, curses.A_BOLD)
        write(1, _pending_header(pending))
        write(2, f"Filter: {query}")
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
        key = screen.getch()
        if key != 27:
            return key
        screen.timeout(25)
        next_key = screen.getch()
        screen.timeout(100)
        if next_key == -1:
            return 27
        if 0 <= next_key <= 255:
            return f"alt-{chr(next_key).lower()}"
        return next_key

    def _run(self, screen, deadline):
        import curses

        screen.keypad(True)
        screen.timeout(100)
        try:
            curses.curs_set(0)
        except curses.error:
            pass
        selected = 0
        query = ""
        while True:
            rows, pending, stopped = self._snapshot()
            if stopped:
                return PresenterResult("", "")
            visible, selected = self._draw(screen, rows, pending, selected, query)
            if time.monotonic() >= deadline:
                return PresenterResult(None, "")
            key = self._read_key(screen)
            if key == -1:
                continue
            if key == 27:
                return PresenterResult("", "")
            if key in (curses.KEY_BACKSPACE, 127, 8):
                query = query[:-1]
                continue
            if visible:
                row = visible[selected]
                if isinstance(key, str):
                    if key in self._action_keys(row):
                        return PresenterResult(key, row)
                    continue
                if 0 <= key <= 255:
                    char = chr(key)
                    if char in self._action_keys(row):
                        return PresenterResult(char, row)
            if key == ord("q"):
                return PresenterResult("", "")
            if key in (curses.KEY_UP, ord("k")) and visible:
                selected = (selected - 1) % len(visible)
                continue
            if key in (curses.KEY_DOWN, ord("j")) and visible:
                selected = (selected + 1) % len(visible)
                continue
            if key in (curses.KEY_ENTER, 10, 13) and visible:
                return PresenterResult("", visible[selected])
            if not visible:
                if isinstance(key, int) and 32 <= key <= 255:
                    query += chr(key)
                continue
            if 0 <= key <= 255:
                char = chr(key)
                if char.isprintable():
                    query += char


class CursesGroupPresenter:
    def __init__(self, group_names):
        self._group_names = list(group_names)
        self._lock = threading.Lock()
        self._rows = []
        self._pending = []
        self._active_group = None
        self._active_presenter = None
        self._stopped = False
        self._reopen_group = None

    def launch(self):
        with self._lock:
            self._stopped = False

    def push_snapshot(self, rows, pending):
        with self._lock:
            self._rows = list(rows)
            self._pending = list(pending)
            group = self._active_group
            presenter = self._active_presenter
            scoped_rows = self._rows_for_group(group, self._rows) if group else []
        if presenter is not None:
            presenter.push_snapshot(scoped_rows, pending)

    def wait_for_exit(self, timeout):
        import curses

        return curses.wrapper(self._run, time.monotonic() + timeout)

    def stop(self):
        with self._lock:
            self._stopped = True
            presenter = self._active_presenter
        if presenter is not None:
            presenter.stop()

    def _rows_for_group(self, group, rows):
        return [
            row.rpartition("\t")[0]
            for row in rows
            if row.rpartition("\t")[2] == group
        ]

    def _snapshot(self):
        with self._lock:
            rows = list(self._rows)
            pending = list(self._pending)
            stopped = self._stopped
        counts = {
            group: sum(1 for row in rows if row.rpartition("\t")[2] == group)
            for group in self._group_names
        }
        return rows, pending, stopped, counts

    def _draw(self, screen, pending, counts, selected):
        import curses

        height, width = screen.getmaxyx()
        screen.erase()

        def write(line, text, attr=0):
            if line < height and width > 1:
                try:
                    screen.addnstr(line, 0, text, width - 1, attr)
                except curses.error:
                    pass

        write(0, "Attention groups", curses.A_BOLD)
        write(1, _pending_header(pending))
        visible_groups = [group for group in self._group_names if counts[group]]
        if not visible_groups:
            write(3, "No items in any group yet.")
        for index, group in enumerate(visible_groups):
            marker = "›" if index == selected else " "
            attr = curses.A_REVERSE if index == selected else 0
            write(3 + index, f"{marker} {group} ({counts[group]})", attr)
        write(height - 1, "↑/↓ select  Enter open group  Esc quit")
        screen.refresh()
        return visible_groups

    def _open_group(self, screen, group, deadline):
        presenter = CursesPresenter(group)
        with self._lock:
            self._active_group = group
            self._active_presenter = presenter
            rows = self._rows_for_group(group, self._rows)
            pending = list(self._pending)
        try:
            presenter.launch()
            presenter.push_snapshot(rows, pending)
            return presenter._run(screen, deadline)
        finally:
            presenter.stop()
            with self._lock:
                if self._active_presenter is presenter:
                    self._active_presenter = None
                    self._active_group = None

    def _open_group_and_track(self, screen, group, deadline):
        result = self._open_group(screen, group, deadline)
        should_return = result.key is None or bool(result.key) or bool(result.row)
        if should_return:
            with self._lock:
                self._reopen_group = group
        return result, should_return

    def _run(self, screen, deadline):
        import curses

        screen.keypad(True)
        screen.timeout(100)
        try:
            curses.curs_set(0)
        except curses.error:
            pass
        selected = 0
        with self._lock:
            reopen_group = self._reopen_group
            self._reopen_group = None
        if reopen_group is not None:
            _, _, stopped, counts = self._snapshot()
            if not stopped and counts.get(reopen_group):
                result, should_return = self._open_group_and_track(screen, reopen_group, deadline)
                if should_return:
                    return result
        while True:
            _, pending, stopped, counts = self._snapshot()
            if stopped:
                return PresenterResult("", "")
            visible_groups = self._draw(screen, pending, counts, selected)
            if visible_groups:
                selected %= len(visible_groups)
            else:
                selected = 0
            if time.monotonic() >= deadline:
                return PresenterResult(None, "")
            key = screen.getch()
            if key in (27, ord("q")):
                return PresenterResult("", "")
            if key in (curses.KEY_UP, ord("k")) and visible_groups:
                selected = (selected - 1) % len(visible_groups)
            elif key in (curses.KEY_DOWN, ord("j")) and visible_groups:
                selected = (selected + 1) % len(visible_groups)
            elif key in (curses.KEY_ENTER, 10, 13) and visible_groups:
                result, should_return = self._open_group_and_track(screen, visible_groups[selected], deadline)
                if should_return:
                    return result
