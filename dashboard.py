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
  output before the presenter redraws. Only presenters that hand an action
  back from `wait_for_exit()` need it: one that accepts an action handler
  (see `Presenter.set_action_handler`) keeps the page up and reports the
  outcome in its notice instead.

An action never interrupts the page. A presenter that supports it is given
`DashboardController.dispatch_action`, which starts the action on a worker
thread and returns at once, so the rows stay on screen: the command's own
output is captured, a `Running: ...` notice appears immediately, and the
result replaces it when the action finishes. An action marked `removes`
(dismiss, complete) takes its row off the page as soon as it is dispatched,
and puts it back if the action fails -- the row would be gone on the next
refresh anyway, so waiting for one only makes the dashboard look stale.

Prompts belong to the presenter too: `ask` renders an action's `input()`
in place and blocks only the worker thread, so answering `c comment` never
leaves the dashboard.

This keeps the dependency direction one-way (`attention` -> `dashboard`,
never back) and lets tests build a `DashboardController` entirely from
fakes -- no real plugin loading, no real CLI process, and no on-disk config.
"""
from __future__ import annotations

import base64
import builtins
import io
import json
import sys
import threading
import time
from typing import Callable, NamedTuple, Protocol


class PresenterResult(NamedTuple):
    key: str | None
    row: str


class Presenter(Protocol):
    def launch(self) -> None: ...
    def push_snapshot(
        self, rows: list[str], pending: list[str], notice: str = "",
    ) -> None: ...
    def wait_for_exit(self) -> PresenterResult: ...
    def stop(self) -> None: ...

    # Optional, and only what makes inline actions possible: a presenter that
    # has them takes an action key directly (never returning it from
    # wait_for_exit) and answers a prompt an action asks for.
    def set_action_handler(self, handler: Callable[[str, str], None]) -> None: ...
    def ask(self, spec: dict) -> str | None: ...


def action_item_identity(action):
    """The (plugin, item id) a row's action belongs to."""
    return (action.get("_plugin"), action.get("_item_id"))


def row_identity(row):
    """The (plugin, item id) of the item a rendered row displays."""
    parts = row.split("\t")
    if len(parts) < 2:
        return None
    try:
        actions = json.loads(base64.b64decode(parts[1]).decode())
    except Exception:
        return None
    for action in actions:
        if isinstance(action, dict) and action.get("_item_id"):
            return action_item_identity(action)
    return None


class _ActionInput:
    """Stand-in for `builtins.input` that draws its prompt in the dashboard.

    Installed while the dashboard owns the terminal so an action's prompt --
    a plugin's own `input()` or `_util`'s configured input spec -- is asked
    in place instead of stepping out to the shell. Canceling raises EOFError,
    which is what the call sites already treat as "Canceled.".
    """

    def __init__(self, ask):
        self._ask = ask
        # `_util.prompt_for_input` looks for this hook on `input` itself.
        self.prompt_spec = ask

    def __call__(self, prompt=""):
        answer = self._ask({"prompt": str(prompt).strip() or "Input"})
        if answer is None:
            raise EOFError("input canceled")
        return answer


def install_input(ask):
    """Route action prompts into the dashboard; returns a restore callable."""
    previous = builtins.input
    builtins.input = _ActionInput(ask)

    def restore():
        builtins.input = previous

    return restore


class _ActionStream:
    """A stdout/stderr that the thread running an action writes into a buffer.

    The dashboard owns the terminal while it is up, so an action's own output
    would otherwise land in the middle of the page. Only the action's own
    thread is diverted: a plugin fetch or any other thread keeps writing to
    the real stream, so nothing else loses its output.
    """

    def __init__(self, stream):
        self._stream = stream
        self._local = threading.local()

    def capture(self, buffer):
        self._local.buffer = buffer

    def release(self):
        self._local.buffer = None

    def _target(self):
        return getattr(self._local, "buffer", None) or self._stream

    def write(self, text):
        self._target().write(text)

    def writelines(self, lines):
        self._target().writelines(lines)

    def flush(self):
        self._target().flush()

    def __getattr__(self, name):
        return getattr(self._stream, name)


def install_output():
    """Scope an action's prints to itself; returns (streams, restore)."""
    originals = (sys.stdout, sys.stderr)
    streams = [_ActionStream(stream) for stream in originals]
    sys.stdout, sys.stderr = streams

    def restore():
        sys.stdout, sys.stderr = originals

    return streams, restore


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
        # The last page actually shown, so an action's notice can be
        # rendered over it without recomputing (and reordering) the list.
        self._last_rows = []
        self._last_pending = []
        self._notice = ""
        # Rows an in-flight action will remove on its own: hidden now, put
        # back if the action fails, and dropped from this set as soon as a
        # fetch of their plugin returns fresh data (which is the truth).
        self._removed = set()
        # Filled in by run() when the presenter can serve an action inline:
        # the stdout/stderr the action's own thread writes into instead of
        # the terminal.
        self._streams = []

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
        set_handler = getattr(self.presenter, "set_action_handler", None)
        if callable(set_handler):
            set_handler(self.dispatch_action)
        ask = getattr(self.presenter, "ask", None)
        restores = []
        try:
            if callable(ask):
                restores.append(install_input(ask))
                self._streams, restore_output = install_output()
                restores.append(restore_output)
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
                self._handle_returned_action(result.key, result.row)
        finally:
            for restore in restores:
                restore()
            with self._presenter_lock:
                with self._state_lock:
                    self._closed = True
                self.presenter.stop()

    def _handle_returned_action(self, key, row):
        """Run an action handed back by a presenter that has no handler.

        This is the pre-handler behavior, kept for presenters (and the CLI)
        that give the terminal back for an action: it blocks until the
        action is done, then redraws the same page with a notice.
        """
        action = self._action_for(key, row)
        item_id = action.get("_item_id") if action else None
        completed = self._act(key, row)
        if completed is not False and action:
            self._invalidate_plugin(action.get("_plugin"))
        self._acknowledge_action()
        if action and not action.get("read"):
            # Read-only actions (open, yank) leave the item in place
            # so it can be acted on again straight away.
            self._deprioritize(item_id)
        self._notice = self._notice_for(action, completed)
        self._repost_page()

    def dispatch_action(self, key, row):
        """Start a row's action without taking the page down.

        Called on the presenter's own thread, so it does as little as
        possible: record the intent (notice, optimistic removal), redraw
        once, and hand the action to a worker thread. The action's stdout
        and stdin belong to the dashboard until it finishes.
        """
        action = self._action_for(key, row)
        if action is None:
            return
        identity = action_item_identity(action)
        with self._state_lock:
            if self._closed:
                return
            label = action.get("label", "")
            self._notice = f"Running: {label}…" if label else "Running…"
            if action.get("removes"):
                self._removed.add(identity)
        self._repost_page()
        threading.Thread(
            target=self._run_action, args=(action, key, row), daemon=True,
        ).start()

    def _run_action(self, action, key, row):
        """Run one action off the UI thread and report what happened.

        The command's output is captured rather than printed: the dashboard
        owns the terminal while the action runs, so stray output would land
        in the middle of the page. Any failure is the action's, not the
        dashboard's -- it is reported as a notice, never raised.
        """
        identity = action_item_identity(action)
        output = io.StringIO()
        for stream in self._streams:
            stream.capture(output)
        try:
            completed = self._act(key, row)
        except Exception as exc:
            completed = False
            output.write(f"{type(exc).__name__}: {exc}\n")
        finally:
            for stream in self._streams:
                stream.release()
        if completed is not False:
            # The plugin's next fetch is the authority on whether the row is
            # really gone, so its optimistic hiding stands until then.
            self._invalidate_plugin(action.get("_plugin"))
        else:
            with self._state_lock:
                # A failed dismissal or completion changed nothing: put the
                # row back where it was.
                self._removed.discard(identity)
        if not action.get("read"):
            self._deprioritize(action.get("_item_id"))
        with self._state_lock:
            self._notice = self._notice_for(action, completed, output.getvalue())
        self._repost_page()

    @staticmethod
    def _notice_for(action, completed, output=""):
        label = (action or {}).get("label", "")
        if completed is False:
            if "Canceled" in output:
                return f"Canceled: {label}" if label else "Canceled"
            return f"Action failed: {label}" if label else "Action failed"
        return f"Done: {label}" if label else "Done"

    def _visible_rows(self, rows):
        with self._state_lock:
            removed = set(self._removed)
        if not removed:
            return list(rows)
        return [row for row in rows if row_identity(row) not in removed]

    def _repost_page(self):
        """Redraw the page the user was already looking at, with a notice.

        An action must not move, remove, or reorder the rows: acting on an
        item and then finding it somewhere else is disorienting. The one
        exception is an action whose item is going away -- a dismissal or a
        completion -- which is dropped as soon as it is dispatched. The
        acted-on item is still deprioritized for the next real refresh.
        """
        rows = self._visible_rows(self._last_rows)
        with self._state_lock:
            pending = list(self._last_pending)
            notice = self._notice
        with self._presenter_lock:
            if self._empty_final:
                return
            with self._state_lock:
                if self._closed:
                    return
            self.presenter.launch()
            self.presenter.push_snapshot(rows, pending, notice)

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
        self._notice = ""
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
                # Fresh data from that plugin is now the truth about which of
                # its rows are gone; nothing needs hiding on its behalf.
                self._removed = {
                    entry for entry in self._removed if entry[0] != name
                }
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
            # Fresh data supersedes the last action's notice.
            with self._state_lock:
                self._notice = ""
            self._push(rows, pending)

    def _push(self, rows, pending):
        # The stored page keeps every row: what is hidden is decided when the
        # page is drawn, so a dismissal that fails can put its row back even
        # if a fetch landed while the action was in flight.
        visible = self._visible_rows(rows)
        with self._state_lock:
            self._last_rows = list(rows)
            self._last_pending = list(pending)
            notice = self._notice
        self.presenter.push_snapshot(visible, pending, notice)

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
            self._push(rows, pending)

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
    """Production presenter backed entirely by the terminal's curses UI.

    An action key is handed to `set_action_handler`'s callback and the page
    stays up, so acting on a row never flashes back to the shell. If no
    handler is registered, the key is returned from `wait_for_exit` instead
    and the caller runs it the old way.

    `ask` is the presenter's other half of the same idea: an action's prompt
    is drawn and answered in place, and only the thread that asked blocks.
    """

    def __init__(self, title="Attention"):
        self._title = title
        self._lock = threading.Lock()
        self._rows = []
        self._pending = []
        self._notice = ""
        self._stopped = False
        self._snapshot_version = 0
        self._last_selection = 0
        self._expanded_action_rows = set()
        self._action_handler = None
        self._prompt = None
        self._prompt_text = ""
        self._prompt_event = None
        self._prompt_answer = None

    def launch(self):
        with self._lock:
            self._stopped = False

    def set_action_handler(self, handler):
        """Take action keys inline instead of returning them to the caller."""
        self._action_handler = handler

    def push_snapshot(self, rows, pending, notice=""):
        with self._lock:
            self._rows = list(rows)
            self._pending = list(pending)
            self._notice = notice
            self._snapshot_version += 1

    def wait_for_exit(self):
        import curses

        return curses.wrapper(self._run)

    def stop(self):
        with self._lock:
            self._stopped = True
            if self._prompt_event is not None:
                self._prompt_answer = None
                self._prompt_event.set()

    def _snapshot(self):
        with self._lock:
            return (
                list(self._rows), list(self._pending), self._notice,
                self._stopped, self._snapshot_version,
                self._prompt, self._prompt_text,
            )

    def dispatch(self, key, row):
        """Give an action key to the handler; False when there is none."""
        handler = self._action_handler
        if handler is None:
            return False
        handler(key, row)
        return True

    def ask(self, spec):
        """Draw `spec`'s prompt in the dashboard and wait for its answer.

        Called from the thread running an action, which blocks here while the
        UI loop collects the answer. Returns None when the user cancels, the
        dashboard is closing, or another prompt is already open -- callers
        treat exactly that as a canceled prompt.
        """
        if not isinstance(spec, dict):
            spec = {"prompt": spec}
        choices = spec.get("choices")
        default = spec.get("default")
        request = {
            "prompt": str(spec.get("prompt") or "Input").strip() or "Input",
            "choices": [str(choice) for choice in choices]
            if isinstance(choices, list) and choices else None,
            "default": None if default is None else str(default),
        }
        with self._lock:
            if self._stopped or self._prompt_event is not None:
                return None
            event = threading.Event()
            self._prompt = request
            self._prompt_text = ""
            self._prompt_event = event
            self._snapshot_version += 1
        event.wait()
        with self._lock:
            answer = self._prompt_answer
            self._prompt = None
            self._prompt_text = ""
            self._prompt_event = None
            self._prompt_answer = None
            self._snapshot_version += 1
        return answer

    def _answer_prompt(self, value):
        with self._lock:
            if self._prompt_event is None:
                return
            self._prompt_answer = value
            self._prompt_event.set()

    def _prompt_key(self, key):
        """Handle a keypress while a prompt is up; True means consumed."""
        import curses

        with self._lock:
            prompt = self._prompt
        if prompt is None:
            return False
        if key == 27:
            self._answer_prompt(None)
            return True
        if key in (curses.KEY_ENTER, 10, 13):
            with self._lock:
                typed = self._prompt_text.strip()
            choices = prompt.get("choices")
            if choices:
                named = [choice for choice in choices if choice.casefold() == typed.casefold()]
                if typed.isdigit() and 1 <= int(typed) <= len(choices):
                    self._answer_prompt(choices[int(typed) - 1])
                elif named:
                    self._answer_prompt(named[0])
                elif not typed and prompt.get("default") in choices:
                    self._answer_prompt(prompt["default"])
            elif typed:
                self._answer_prompt(typed)
            elif prompt.get("default") is not None:
                self._answer_prompt(prompt["default"])
            return True
        if key in (curses.KEY_BACKSPACE, 127, 8):
            with self._lock:
                self._prompt_text = self._prompt_text[:-1]
            return True
        if 0 <= key <= 255:
            char = chr(key)
            choices = prompt.get("choices")
            if choices and char.isdigit() and 1 <= int(char) <= len(choices):
                self._answer_prompt(choices[int(char) - 1])
                return True
            if char.isprintable():
                with self._lock:
                    self._prompt_text += char
        return True

    def _prompt_render(self, prompt, text, width):
        """The prompt's status line and footer hints, or (None, None)."""
        if prompt is None:
            return None, None
        import textwrap

        choices = prompt.get("choices")
        if not choices:
            default = prompt.get("default")
            status = f"{prompt['prompt']} {text}▌"
            if default:
                hint = f'Enter accepts "{default}" · Esc cancels'
            else:
                hint = "Enter submit · Esc cancel"
            return status, [hint]
        parts = [f"{index}) {choice}" for index, choice in enumerate(choices, 1)]
        return prompt["prompt"], textwrap.wrap(
            "  ".join(parts) + "  · Esc cancel",
            width=max(1, width - 1), replace_whitespace=False,
        )

    @staticmethod
    def _status_line(pending, notice):
        if notice:
            return notice
        return _pending_header(pending)

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

    def _draw(self, screen, rows, pending, selected, query, notice="", prompt=None, prompt_text=""):
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
        prompt_status, prompt_hints = self._prompt_render(prompt, prompt_text, width)
        if prompt_hints is not None:
            hint_lines = prompt_hints
        else:
            hint_lines = self._display_hints(visible[selected], width) if visible else []
        footer_start = max(3, height - max(1, len(hint_lines)))
        capacity = max(0, footer_start - 3)
        start = min(max(0, selected - capacity + 1), max(0, len(visible) - capacity))

        write(0, self._title, curses.A_BOLD)
        if prompt_status is not None:
            write(1, prompt_status, curses.A_BOLD)
        else:
            write(1, self._status_line(pending, notice))
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
            rows, pending, notice, stopped, version, prompt, prompt_text = self._snapshot()
            if stopped:
                return PresenterResult("", "")
            visible, selected = self._draw(
                screen, rows, pending, selected, query, notice, prompt, prompt_text,
            )
            key = self._read_key(screen)
            if key != -1 and self._prompt_key(key):
                continue
            if query is None and key == ord("r"):
                return PresenterResult("r", "")
            if key == -1:
                # Check for a new snapshot pushed by the controller
                # (e.g. after an action) without exiting the loop.
                _, _, _, stopped2, version2, _, _ = self._snapshot()
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
                    if self.dispatch(char, visible[selected]):
                        continue
                    return PresenterResult(char, visible[selected])
            if key in (curses.KEY_ENTER, 10, 13) and visible:
                self._last_selection = selected
                if self.dispatch("", visible[selected]):
                    continue
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

    def _draw_tree(self, screen, entries, pending, selected, query, notice="", prompt=None, prompt_text=""):
        import curses

        height, width = screen.getmaxyx()
        screen.erase()
        if entries:
            selected = min(selected, len(entries) - 1)
        else:
            selected = 0
        item = entries[selected][1] if entries else None
        prompt_status, prompt_hints = self._prompt_render(prompt, prompt_text, width)
        if prompt_hints is not None:
            hints = prompt_hints
        elif item:
            hints = self._display_hints(item, width)
        else:
            hints = ["Enter expand/collapse · / filter · r refresh · Esc quit"]
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
        if prompt_status is not None:
            write(1, prompt_status, curses.A_BOLD)
        else:
            write(1, self._status_line(pending, notice))
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
            rows, pending, notice, stopped, _, prompt, prompt_text = self._snapshot()
            if stopped:
                return PresenterResult("", "")
            entries = self._entries(rows, query)
            selected = self._draw_tree(
                screen, entries, pending, selected, query, notice, prompt, prompt_text,
            )
            key = self._read_key(screen)
            if key == -1:
                continue
            if self._prompt_key(key):
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
                    if self.dispatch("", row):
                        continue
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
                    if self.dispatch(char, row):
                        continue
                    return PresenterResult(char, row)
            if key == ord("/"):
                query = ""
                continue
            if query is not None and 0 <= key <= 255:
                char = chr(key)
                if char.isprintable() and not ("A" <= char <= "Z") and char not in "jk":
                    query += char
