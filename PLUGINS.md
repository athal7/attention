# Writing a plugin

`attention`'s core has no source-specific logic at all -- it loads
whichever plugins `config["plugins"]` names, calls each one's `fetch()`,
merges/prioritizes/renders what comes back, and routes hotkey presses to
whichever plugin's action produced the pressed key. The four bundled
sources (`sources/calendar.py`, `sources/reminders.py`,
`sources/github.py`, `sources/linear.py`) are ordinary plugins written
against this same interface -- read one of them for a working example.

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

### Enforced Item and Action shape

Runtime type validation is strictly enforced at the plugin boundary. If a plugin's `fetch()` returns a malformed item or an item with malformed actions, a detailed `ValueError` is raised naming the plugin and the specific problem.

The following shows the expected types, which fields are required/optional, and the default values supplied if optional fields are omitted.

#### Item fields
- **`status`** (`str`, **required**): Short, normalized, all-caps status convention (e.g., `"REVIEW REQUESTED"`).
- **`context`** (`str`, **required**): Where the item originated (e.g., repo, list, calendar, project).
- **`title`** (`str`, **required**): The title/headline of the item.
- **`details`** (`str`, **required**): Trailing free-form text or description (may be empty).
- **`weight`** (`int`, **required**): Sort priority key, descending.
- **`id`** (`str`, *optional*, defaults to `""`): Unique identifier; enables cross-link merging (see below).
- **`absorb_note`** (`str`, *optional*, defaults to `""`): Note appended to host's details if this item is absorbed.
- **`actions`** (`list` of action dicts, *optional*, defaults to `[]`): Actions available for this item.

#### Action fields
- **`key`** (`str`, **required**): The hotkey token (e.g., `"alt-o"`, a bare letter/digit, or `""`).
- **`label`** (`str`, **required**): Short verb shown in the footer hint.
- **`primary`** (`bool`, *optional*, defaults to `False`): At most one per item; indicates what plain Enter runs.
- **`payload`** (`dict`, *optional*, defaults to `{}`): Arbitrary dictionary of data passed back to `act()`. The `payload` received by `act()` is validated to be a dictionary at runtime before dispatching.

```python
{
    "status":  "REVIEW REQUESTED",   # str, required
    "context": "myorg/myrepo",       # str, required
    "title":   "Fix the login bug",  # str, required
    "details": "",                   # str, required -- may be empty
    "weight":  90,                   # int, required -- sort key, descending
    "id":      "42",                 # str, optional (defaults to "")
    "absorb_note": "...",            # str, optional (defaults to "")
    "actions": [                     # list, optional (defaults to [])
        {
            "key": "alt-o",          # str, required
            "label": "open",         # str, required
            "primary": True,         # bool, optional (defaults to False)
            "payload": {...},        # dict, optional (defaults to {})
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
  filesystem path to your own module -- nothing to register, register,
  or install; just point at the file.

```json
{ "plugins": ["calendar", "reminders", "github", "linear", "/Users/you/attention-plugins/jira.py"] }
```

Each plugin owns its own config however it likes -- by convention, put
your settings under a top-level key matching your plugin's name (e.g.
`config["jira"]`), since that's what the bundled plugins do, but nothing
in core enforces this.
