# Writing a plugin

`attention`'s core has no source-specific logic at all -- it loads
whichever plugins `config["plugins"]` names, calls each one's `fetch()`,
merges/prioritizes/renders what comes back, and routes hotkey presses to
whichever plugin's action produced the pressed key. The five bundled
sources (`sources/calendar.py`, `sources/reminders.py`,
`sources/github.py`, `sources/linear.py`, `sources/generic.py`) are
ordinary plugins written against this same interface -- read one of
them for a working example. `generic.py` is itself a config-only
provider engine; see "Config-only providers" below before writing a new
`.py` file for a source that's just "run a command, map its output".

## Interface

A plugin is a Python module (a single `.py` file) exposing two functions:

```python
def fetch(config: dict) -> list[dict]:
    """Return the items this plugin currently sees. `config` is the
    full attention config -- look up your own section by whatever name
    you're registered under in config["plugins"].
    """

def act(key: str, payload) -> None:
    """Run the action for `key`. Always the exact key you declared in
    fetch() -- if this action got absorbed into another item and its key
    was renamed to dodge a collision (see below), you still receive your
    original key here, never the renamed one; core tracks that mapping
    for you. `payload` is exactly whatever value you attached to that
    action's "payload" field in fetch() -- round-tripped unchanged
    (JSON-serialized in between), yours to shape however you like. Print
    whatever the user should see; raise to report failure (printed as
    "Action failed: ...", not a crash).
    """
```

### Item shape

Enforced at the plugin boundary: `fetch_all()` validates every item (and
its actions) immediately after a plugin's `fetch()` returns, raising
`ValueError(f"plugin '<name>' returned a malformed item: <detail>")`
naming the plugin and the exact problem, rather than letting a bad shape
propagate into `merge_cross_links()`/`render_rows()` and fail opaquely
several frames later. `act()`'s payload is validated the same way before
dispatch. Required fields default to nothing (must be present); optional
fields are filled in with the defaults shown below if omitted.

```python
{
    "status":  "REVIEW REQUESTED",   # str, required -- normalized, all-caps by convention
    "context": "myorg/myrepo",       # str, required -- where this came from
    "title":   "Fix the login bug",  # str, required
    "details": "",                   # str, required -- may be empty
    "weight":  90,                   # int, required -- sort key, descending (bool rejected)
    "id":      "42",                 # str, optional (default "") -- enables cross-link merging, see below
    "absorb_note": "...",            # str, optional (default "") -- text used if THIS item gets merged into another
    "actions": [                     # list, optional (default [])
        {
            "key": "alt-o",          # str, required -- fzf --expect token: "alt-X", a bare letter/digit, or ""
            "label": "open",         # str, required -- short verb, shown in the footer hint
            "primary": True,         # bool, optional (default False) -- at most one per item, what plain Enter runs
            "payload": {...},        # dict, optional (default {}) -- anything JSON-serializable; yours, passed back to act()
        },
        ...
    ],
}
```

Hotkeys are conventionally `alt-<letter>` (fzf's `alt-X` syntax) so plain
letters stay free for fzf's fuzzy-filter typing. Bare letters/digits work
too (used for overflow cases -- see `sources/calendar.py`'s handling of
more than one linked reminder) but cost query-filtering characters, so
prefer `alt-` unless you have a specific reason not to (e.g. matching an
already-established convention elsewhere).

### Cross-link merging

Core runs two generic, source-agnostic passes over every fetched item,
regardless of which plugin produced it:

1. **ID-shaped title match**: if item A's title contains a token shaped
   like `[A-Z]+-\d+` (e.g. `ABC-123`) and some other item B's `id`
   (case-insensitive) equals that token, B is absorbed into A.
2. **Title-substring match**: if item A's title (≥4 chars, not
   "untitled") is a substring of item B's title, B is absorbed into A.

"Absorbed" means: B's `absorb_note` (or a generic status/title fallback)
gets appended to A's `details`; A's `weight` becomes
`max(A.weight, B.weight) + 5`; and B's `actions` get appended to A's,
each renamed only if its key would otherwise collide with one A already
has (tried in order: the same key stripped of `alt-` and uppercased, then
the lowest unused bare digit) and labeled `"<label> (linked)"`. B itself
is dropped from the rendered list.

## Registering

`config["plugins"]` is an ordered list of plugin identifiers:

- A bare name (e.g. `"github"`) resolves to a bundled
  `sources/<name>.py` next to the `attention` script.
- Anything containing `/` or ending in `.py` is treated as a direct
  filesystem path to your own module -- nothing to register or install;
  just point at the file.

```json
{ "plugins": ["calendar", "reminders", "github", "linear", "/Users/you/attention-plugins/jira.py"] }
```

Each plugin owns its own config however it likes -- by convention, put
your settings under a top-level key matching your plugin's name (e.g.
`config["jira"]`), since that's what the bundled plugins do, but nothing
in core enforces this.


## Config-only providers (no Python required)

For a source that's just "run a command, map its JSON output onto the
item shape" -- no cross-repo resolution, no multi-query de-duping, no
bespoke logic -- you don't need a `.py` file at all. Add `"generic"` to
`config["plugins"]` and define as many named providers as you like
under `config["generic"]`:

```json
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
        {"key": "alt-o", "label": "open", "primary": true, "command": ["open", "{url}"]}
      ]
    }
  }
}
```

`command` must print a JSON array to stdout; each element is one
record. Every text field (`status`/`context`/`title`/`details`/`id`,
and each action's `command` tokens) is a template: plain text is used
as-is, and `{dotted.path}` substitutes that field from the record
(missing paths become `""`). `weight` is a plain int, or a `{path}`
template parsed as one (falling back to `50` if that fails). An action
may set `"background": true` to dispatch via `dispatch_background`
(fire-and-forget, e.g. starting a long-running session) instead of the
default `run_cmd` (blocks, prints failures).

Each named provider is fetched and mapped independently -- one with a
missing/failing `command` or non-JSON-array output contributes nothing,
without affecting the others.