"""dashboard -- the interactive dashboard's progressive-fetch controller.

Standalone module: no import of `attention` anywhere in this file (and
none of `subprocess`/`socket`/`http.client` either -- those live only in
`FzfPresenter`, below). `attention`, being extensionless, can't be
`import`ed as an ordinary module anyway; instead `attention.run_dashboard()`
constructs a `DashboardController` and hands it every core operation it
needs as a plain callable:

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
fakes -- no real plugin loading, no real `gh`/`fzf` process, no on-disk
config.
"""
import base64
import http.client
import json
import os
import shutil
import socket
import subprocess
import tempfile
import threading
import time
from typing import Callable, NamedTuple, Protocol


class PresenterResult(NamedTuple):
    key: str | None  # None means wait_for_exit's timeout elapsed
    row: str


class Presenter(Protocol):
    def launch(self, expect_keys: list[str], header: str) -> None: ...
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


def build_launch_binds(universe_keys):
    """The launch-time `--bind` values for the declared hotkey universe:
    one `KEY:print(KEY)+accept` per key -- reproducing `--expect`'s
    "accept and report which key" output shape without `--expect`
    itself -- plus an explicit `enter:print()+accept` so plain Enter
    keeps producing the same two-line shape `act()` already parses.
    """
    binds = [f"{key}:print({key})+accept" for key in universe_keys]
    binds.append("enter:print()+accept")
    return binds


def build_focus_transform(universe_keys):
    """The shell snippet run by the `start,focus` `transform` bind:
    unconditionally unbinds every key in the declared universe, then --
    only when the newly-focused row's field-3 CSV (`{3}`) is non-empty --
    rebinds exactly those keys, restoring their launch-time
    `print(KEY)+accept` binding. A key that isn't rebound falls back to
    fzf's own default per-character behavior (ordinary query typing for
    a letter/digit), never an unconditional accept.

    A row with no actions at all (empty field 3) must never produce
    `rebind()`: fzf requires a non-empty target for `rebind(...)` and
    parses the whole transform result before running any of it, so an
    empty `rebind()` makes fzf discard the entire result -- leaving
    whichever binding the previously-focused row's transform left
    active still active. Emitting `unbind(...)` alone for an action-less
    row is therefore both correct and necessary, not just tidier.
    """
    universe_csv = ",".join(universe_keys)
    return (
        f'k={{3}}; if [ -z "$k" ]; then printf "unbind({universe_csv})"; '
        f'else printf "unbind({universe_csv})+rebind(%s)" "$k"; fi'
    )


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
        self, plugin_names, presenter, expect_keys, *,
        fetch_plugin: Callable[[str], list],
        build_snapshot: Callable[[dict, dict], list],
        render_rows: Callable[[list], list],
        act: Callable[[str, str], None],
    ):
        self.plugin_names = list(plugin_names)
        self.presenter = presenter
        self.expect_keys = list(expect_keys)
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
            self.presenter.launch(self.expect_keys, _pending_header(pending))
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


class CursesGroupPresenter:
    def __init__(self, group_names):
        self._group_names = list(group_names)
        self._lock = threading.Lock()
        self._rows = []
        self._pending = []
        self._expect_keys = []
        self._header = ""
        self._active_group = None
        self._active_fzf = None
        self._stopped = False

    def launch(self, expect_keys, header):
        with self._lock:
            self._expect_keys = list(expect_keys)
            self._header = header
            self._stopped = False

    def push_snapshot(self, rows, pending):
        with self._lock:
            self._rows = list(rows)
            self._pending = list(pending)
            group = self._active_group
            presenter = self._active_fzf
            scoped_rows = self._rows_for_group(group, self._rows) if group else []
        if presenter is not None:
            presenter.push_snapshot(scoped_rows, pending)

    def wait_for_exit(self, timeout):
        import curses

        return curses.wrapper(self._run, time.monotonic() + timeout)

    def stop(self):
        with self._lock:
            self._stopped = True
            presenter = self._active_fzf
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
        import curses

        presenter = FzfPresenter()
        with self._lock:
            self._active_group = group
            self._active_fzf = presenter
            expect_keys = list(self._expect_keys)
            header = f"{group} · {self._header}"
            rows = self._rows_for_group(group, self._rows)
            pending = list(self._pending)
        curses.def_prog_mode()
        curses.endwin()
        try:
            presenter.launch(expect_keys, header)
            with self._lock:
                rows = self._rows_for_group(group, self._rows)
                pending = list(self._pending)
            presenter.push_snapshot(rows, pending)
            return presenter.wait_for_exit(max(0.01, deadline - time.monotonic()))
        finally:
            presenter.stop()
            with self._lock:
                if self._active_fzf is presenter:
                    self._active_fzf = None
                    self._active_group = None
            curses.reset_prog_mode()
            screen.refresh()

    def _run(self, screen, deadline):
        import curses

        screen.keypad(True)
        screen.timeout(100)
        try:
            curses.curs_set(0)
        except curses.error:
            pass
        selected = 0
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
                result = self._open_group(screen, visible_groups[selected], deadline)
                if result.key is None:
                    return result
                if result.key or result.row:
                    return result
class UnixHTTPConnection(http.client.HTTPConnection):
    """`http.client.HTTPConnection` whose `connect()` opens an `AF_UNIX`
    socket at a fixed path instead of resolving a host:port over TCP --
    the transport fzf's `--listen <path>` server also speaks. stdlib
    only, no `curl` dependency.
    """

    def __init__(self, socket_path, timeout=5):
        super().__init__("localhost", timeout=timeout)
        self._socket_path = socket_path

    def connect(self):
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        sock.connect(self._socket_path)
        self.sock = sock


def unix_request(socket_path, method, path, body=None, timeout=5):
    """One request/response round trip over a Unix domain socket,
    returning `(status, body_bytes)`. Used both by `FzfPresenter`
    (POSTing `reload`/`change-header`/`abort` actions to fzf's
    `--listen` server) and by tests driving its GET state endpoint.
    """
    conn = UnixHTTPConnection(socket_path, timeout=timeout)
    try:
        conn.request(method, path, body=body)
        resp = conn.getresponse()
        return resp.status, resp.read()
    finally:
        conn.close()


class PresenterLaunchTimeout(RuntimeError):
    """Raised by `FzfPresenter.launch()` when the spawned `fzf` process
    never creates its `--listen` socket within the readiness timeout.
    The caller is left with no live process or temp directory to clean
    up itself -- `launch()` has already terminated the process and
    removed the temp directory before this propagates.
    """


def _terminate_and_reap(proc, timeout=2):
    """SIGTERMs `proc`, escalating to SIGKILL if it hasn't exited within
    `timeout` seconds, and always reaps it (blocks until gone).
    """
    proc.terminate()
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()


class FzfPresenter:
    """Production `Presenter`: one real `fzf` 0.74.2 process per launch,
    fed snapshots over a per-launch Unix domain socket `--listen`
    server (Decision 2 in design.md) instead of a static stdin pipe.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._proc = None
        self._tmpdir = None
        self._sock_path = None
        self._snapshot_path = None

    def launch(self, expect_keys, header, *, timeout=5):
        self._teardown_current(kill_only=True)
        tmpdir = tempfile.mkdtemp(prefix="attention-dashboard-")
        sock_path = os.path.join(tmpdir, "fzf.sock")
        focus_cmd = build_focus_transform(expect_keys)
        args = [
            "fzf", "--ansi", "--layout=reverse", "--height", "10",
            "-d", "\t", "--with-nth", "1", "--listen", sock_path,
            "--footer-border=line", "--header", header,
        ]
        for bind in build_launch_binds(expect_keys):
            args += ["--bind", bind]
        args += ["--bind", 'start,focus:transform[' + focus_cmd + ']+transform-footer:printf "%s\\n" {4}']
        try:
            proc = subprocess.Popen(
                args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, text=True,
            )
        except OSError:
            shutil.rmtree(tmpdir, ignore_errors=True)
            raise
        try:
            self._wait_for_socket(sock_path, timeout=timeout)
        except PresenterLaunchTimeout:
            _terminate_and_reap(proc)
            shutil.rmtree(tmpdir, ignore_errors=True)
            raise
        with self._lock:
            self._proc = proc
            self._tmpdir = tmpdir
            self._sock_path = sock_path

    def _wait_for_socket(self, sock_path, timeout=5):
        deadline = time.monotonic() + timeout
        delay = 0.01
        while time.monotonic() < deadline:
            if os.path.exists(sock_path):
                return
            time.sleep(delay)
            delay = min(delay * 2, 0.2)
        raise PresenterLaunchTimeout(
            f"fzf never created its --listen socket at {sock_path!r} within {timeout}s"
        )

    def push_snapshot(self, rows, pending):
        with self._lock:
            tmpdir, sock_path = self._tmpdir, self._sock_path
        if not sock_path:
            return
        try:
            fd, path = tempfile.mkstemp(dir=tmpdir, prefix="snapshot-", suffix=".tsv")
            with os.fdopen(fd, "w") as f:
                f.write("\n".join(rows))
        except OSError:
            # tmpdir was torn down (wait_for_exit's timeout/exit path raced
            # ahead of us -- see _clear_state_if_current()) between our read
            # of it above and this mkstemp; the presenter is gone, so there's
            # nothing left to publish to.
            return
        body = f'reload[cat "{path}"]+first+change-header:{_pending_header(pending)}'
        try:
            unix_request(sock_path, "POST", "/", body=body.encode())
        except OSError:
            try:
                os.remove(path)
            except OSError:
                pass
            return
        with self._lock:
            previous_path = self._snapshot_path
            self._snapshot_path = path
        if previous_path:
            try:
                os.remove(previous_path)
            except OSError:
                pass

    def wait_for_exit(self, timeout):
        with self._lock:
            proc, tmpdir = self._proc, self._tmpdir
        if proc is None:
            return PresenterResult("", "")
        try:
            out, _ = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            _terminate_and_reap(proc)
            self._clear_state_if_current(proc)
            shutil.rmtree(tmpdir, ignore_errors=True)
            return PresenterResult(None, "")
        self._clear_state_if_current(proc)
        shutil.rmtree(tmpdir, ignore_errors=True)
        if not out.strip():
            return PresenterResult("", "")
        first_nl = out.find("\n")
        if first_nl == -1:
            return PresenterResult(out.rstrip("\n"), "")
        return PresenterResult(out[:first_nl], out[first_nl + 1:].rstrip("\n"))

    def _clear_state_if_current(self, proc):
        """Clears the live-presenter fields as soon as `proc` is known to
        have exited (or been reaped after a timeout), so a `push_snapshot()`
        call racing against this teardown sees a cleared `sock_path` and
        returns immediately instead of reading a `tmpdir` this method is
        about to (or has just) removed. Guarded by identity, not just
        presence, so this can never clobber a newer launch()'s state.
        """
        with self._lock:
            if self._proc is proc:
                self._proc = None
                self._tmpdir = None
                self._sock_path = None
                self._snapshot_path = None

    def stop(self):
        self._teardown_current(kill_only=False)

    def _teardown_current(self, kill_only):
        with self._lock:
            proc, tmpdir, sock_path = self._proc, self._tmpdir, self._sock_path
            self._proc = None
            self._tmpdir = None
            self._sock_path = None
            self._snapshot_path = None
        if proc is not None and proc.poll() is None:
            aborted = False
            if sock_path and not kill_only:
                try:
                    unix_request(sock_path, "POST", "/", body=b"abort", timeout=2)
                    aborted = True
                except OSError:
                    aborted = False
            if aborted:
                try:
                    proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    aborted = False
            if not aborted:
                _terminate_and_reap(proc)
        if tmpdir:
            shutil.rmtree(tmpdir, ignore_errors=True)
