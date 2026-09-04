"""github plugin -- PRs needing your review or attention, and issues
assigned to you or open in repos you own, via the `gh` CLI
(https://cli.github.com, already authenticated).

Also surfaces unread GitHub notifications (`mention`, `author`,
`state_change`, `ci_activity`) via `gh api /notifications`, which
catches items the search-based queries miss: direct mentions,
comments on your PRs that aren't review comments, state changes
on subscribed PRs, and CI failures on watched repos.

Config (config["github"]): none required. codeDir (top-level, shared
with other repo-resolving plugins) is used to resolve each item's local
checkout. botReviewAllowlist optionally re-admits specific bot logins
(e.g. "coderabbitai[bot]", with or without the suffix) into the "needs
attention" review-comment check, which otherwise ignores every
reviewer GitHub's GraphQL API reports as a Bot actor.
"""
import concurrent.futures
import json
import os
import re
import subprocess
import threading
from pathlib import Path

from _util import resolve_configured_actions, run_cmd, run_configured_action, slugify

_MAX_WORKERS = 8

_MAX_PR_DETAIL_WORKERS = 32
_PR_DETAIL_FIELDS = (
    "mergeable,reviewDecision,statusCheckRollup,latestReviews,"
    "closingIssuesReferences,isDraft,reviewRequests,baseRefName"
)
_repo_dir_indexes = {}
_repo_dir_indexes_lock = threading.Lock()


def _pr_key(pr):
    repo = pr.get("repository", {}).get("nameWithOwner", "")
    number = pr.get("number")
    if not repo or number is None:
        return None
    return repo.casefold(), str(number)


def _fetch_pr_detail(repo, number):
    detail = _gh_json([
        "pr", "view", str(number), "-R", repo, "--json", _PR_DETAIL_FIELDS,
    ])
    return detail if isinstance(detail, dict) else None


def _body_association_keys(body):
    return [
        f"linear:{identifier.upper()}"
        for identifier in re.findall(r"\b[A-Z][A-Z0-9]+-\d+\b", body or "", flags=re.IGNORECASE)
    ]


def _gh_json(args):
    """Run `gh <args...>` and parse its stdout as JSON, returning [] on
    any failure (non-zero exit, timeout, malformed JSON).
    """
    try:
        res = subprocess.run(["gh"] + args, capture_output=True, text=True, timeout=30)
        if res.returncode != 0:
            return []
        return json.loads(res.stdout or "[]")
    except Exception:
        return []


def _get_gh_login():
    try:
        res = subprocess.run(["gh", "api", "user", "--jq", ".login"], capture_output=True, text=True)
        if res.returncode == 0:
            return res.stdout.strip()
    except Exception:
        pass
    return ""


def _is_bot_login(login):
    # Fallback only, used when _fetch_review_bot_flags() can't be reached:
    # GitHub's GraphQL `Bot` actor type never actually carries this
    # suffix on its bare `login` (see _fetch_review_bot_flags), so this
    # heuristic under-detects on its own.
    return login.casefold().endswith("[bot]")


def _fetch_review_bot_flags(repo, number):
    """login (casefold) -> True if that PR review's author is a GraphQL
    `Bot` actor. `gh pr view --json latestReviews` flattens each review's
    author down to a bare `login`, dropping GraphQL's `__typename` -- and
    a bot's `login` there never carries the "[bot]" suffix REST/UI
    surfaces show (e.g. Copilot's code-review account is
    "copilot-pull-request-reviewer", CodeRabbit's is "coderabbitai",
    dependabot's is "dependabot"), so a suffix check alone silently lets
    every bot review through as if a person wrote it. This raw GraphQL
    query is the only way to recover the actor type.
    """
    owner, _, name = repo.partition("/")
    if not owner or not name:
        return {}
    result = _gh_json([
        "api", "graphql",
        "-f", "query=query($owner:String!,$name:String!,$number:Int!){"
              "repository(owner:$owner,name:$name){pullRequest(number:$number){"
              "latestReviews(first:50){nodes{author{__typename login}}}}}}",
        "-f", f"owner={owner}",
        "-f", f"name={name}",
        "-F", f"number={number}",
    ])
    repository = ((result or {}).get("data") or {}).get("repository") or {}
    pr = repository.get("pullRequest") or {}
    nodes = (pr.get("latestReviews") or {}).get("nodes") or []
    flags = {}
    for node in nodes:
        author = node.get("author") or {}
        login = (author.get("login") or "").casefold()
        if login:
            flags[login] = author.get("__typename") == "Bot"
    return flags

def _default_branch(repo, branches, lock):
    with lock:
        if repo in branches:
            return branches[repo]
        data = _gh_json(["repo", "view", repo, "--json", "defaultBranchRef"])
        branch = ((data or {}).get("defaultBranchRef") or {}).get("name", "")
        branches[repo] = branch
        return branch


def _pr_indicators(detail, is_draft, default_branch):
    checks = detail.get("statusCheckRollup") or []
    failing_checks = {"FAILURE", "ERROR", "CANCELLED", "TIMED_OUT", "ACTION_REQUIRED", "STARTUP_FAILURE"}
    complete_checks = {"SUCCESS", "NEUTRAL", "SKIPPED"}
    conclusions = {check.get("conclusion") for check in checks}
    if conclusions & failing_checks:
        ci = "×"
    elif checks and conclusions <= complete_checks:
        ci = "✓"
    elif checks:
        ci = "…"
    else:
        ci = "—"

    review_states = {review.get("state") for review in detail.get("latestReviews") or []}
    if "CHANGES_REQUESTED" in review_states:
        review = "×"
    elif "COMMENTED" in review_states:
        review = "!"
    elif detail.get("reviewDecision") == "APPROVED":
        review = "✓"
    elif detail.get("reviewRequests") or detail.get("reviewDecision") == "REVIEW_REQUIRED":
        review = "…"
    else:
        review = "—"

    mergeable = detail.get("mergeable")
    merge = "×" if mergeable == "CONFLICTING" else "✓" if mergeable == "MERGEABLE" else "…"

    base_branch = detail.get("baseRefName", "")
    if not base_branch or not default_branch:
        stacked = "—"
    else:
        stacked = "×" if base_branch == default_branch else "✓"

    return {
        "ci": ci,
        "ready": "×" if is_draft else "✓",
        "review": review,
        "merge": merge,
        "stacked": stacked,
    }

def _status_badge(gtype, reasons, is_draft, review_requested):
    if "Merge Conflict" in reasons:
        return "Merge ❌"
    if "Changes Requested" in reasons:
        return "Changes ❌"
    if "Checks Failing" in reasons:
        return "CI ❌"
    if "Review Commented" in reasons:
        return "Reply ⏳"
    if gtype == "review_request" or review_requested:
        return "Review ⏳"
    if is_draft:
        return "Draft ⏳"
    if gtype == "assigned_issue":
        return "Assigned ⏳"
    if gtype == "repo_issue":
        return "Triage ⏳"
    if gtype == "notification":
        return "Reply ⏳"
    return "Ready ✅"


def _classify_pr_attention(
    pr, expected_author, bot_review_allowlist, default_branches, default_branch_lock, detail,
):
    key = _pr_key(pr)
    if key is None or detail is None:
        return None
    repo, number = pr["repository"]["nameWithOwner"], pr["number"]
    reasons = []
    latest_review_states = set()
    bot_flags = None
    for review in detail.get("latestReviews") or []:
        reviewer = review.get("author", {}).get("login") or ""
        if not reviewer or reviewer.casefold() == expected_author.casefold():
            continue
        reviewer_cf = reviewer.casefold()
        if bot_flags is None:
            bot_flags = _fetch_review_bot_flags(repo, number)
        is_bot = bot_flags.get(reviewer_cf)
        if is_bot is None:
            is_bot = _is_bot_login(reviewer)
        if is_bot:
            canonical = reviewer_cf if reviewer_cf.endswith("[bot]") else f"{reviewer_cf}[bot]"
            if canonical not in bot_review_allowlist and reviewer_cf not in bot_review_allowlist:
                continue
        latest_review_states.add(review.get("state"))
    if "CHANGES_REQUESTED" in latest_review_states:
        reasons.append("Changes Requested")
    if "COMMENTED" in latest_review_states:
        reasons.append("Review Commented")
    if detail.get("mergeable") == "CONFLICTING":
        reasons.append("Merge Conflict")
    checks = detail.get("statusCheckRollup") or []
    if any(c.get("conclusion") in ("FAILURE", "ERROR") for c in checks):
        reasons.append("Checks Failing")
    if not reasons:
        return None
    pr = dict(pr)
    pr["reviewRequested"] = bool(detail.get("reviewRequests"))
    pr["closingIssuesReferences"] = detail.get("closingIssuesReferences") or []
    pr["isDraft"] = detail.get("isDraft", pr.get("isDraft", False))
    pr["attention_reasons"] = reasons
    default_branch = _default_branch(repo, default_branches, default_branch_lock) if detail.get("baseRefName") else ""
    pr["indicators"] = _pr_indicators(
        detail, detail.get("isDraft", pr.get("isDraft", False)), default_branch,
    )
    return pr


def _fetch_pr_attention(
    author, detail_pool, bot_review_allowlist=frozenset(),
    default_branches=None, default_branch_lock=None, current_login=None, detail_lookup=None,
):
    prs = _gh_json([
        "search", "prs", f"--author={author}", "--state=open", "--archived=false", "--limit", "50",
        "--json", "number,title,body,repository,url,isDraft,createdAt",
    ])
    if not prs:
        return []
    default_branches = {} if default_branches is None else default_branches
    default_branch_lock = threading.Lock() if default_branch_lock is None else default_branch_lock
    expected_author = current_login if current_login is not None else _get_gh_login()
    details = {}
    if detail_lookup is None:
        details = {
            key: detail_pool.submit(_fetch_pr_detail, pr["repository"]["nameWithOwner"], pr["number"])
            for pr in prs if (key := _pr_key(pr)) is not None
        }
    def detail_for(pr):
        key = _pr_key(pr)
        if key is None:
            return None
        if detail_lookup is not None:
            return detail_lookup(pr)
        return details[key].result()
    return [
        result for pr in prs
        if (result := _classify_pr_attention(
            pr, expected_author, bot_review_allowlist, default_branches, default_branch_lock,
            detail_for(pr),
        )) is not None
    ]


def _fetch_my_repo_issues():
    """Return only unassigned issues in owned repositories for triage."""
    issues = _gh_json([
        "search", "issues", "--owner=@me", "--state=open", "--archived=false", "--limit", "50",
        "--json", "number,title,repository,url,createdAt,assignees",
    ])
    return [issue for issue in issues or [] if not issue.get("assignees")]


def _fetch_notifications():
    """Unread notifications from `gh api /notifications`. Covers reasons
    the search-based queries above miss: `mention` (direct mentions),
    `author` (comments on PRs I authored that aren't review comments),
    `state_change` (state changes on PRs I'm subscribed to), and
    `ci_activity` (CI failures on repos I watch). `review_requested`
    notifications overlap with the review-requested search; de-dup
    handles that.

    Each notification subject maps to the same (repo, number) item
    shape the search queries produce, so the shared de-dup logic works.
    CheckSuite subjects have no number; they surface as CI-failure
    items keyed by the repo alone with a synthetic number derived from
    the title so they don't collide with real PRs/issues.
    """
    notifications = _gh_json(["api", "/notifications", "--paginate"])
    if not notifications:
        return []

    # Phase 1: early filter to only mention/author, then batch-check
    # archived repos in parallel (27 unique repos instead of 497 sequential).
    filtered = [n for n in notifications if n.get("unread") and n.get("reason") in {"mention", "author"}]
    repo_names = list({n.get("repository", {}).get("full_name", "") for n in filtered if n.get("repository", {}).get("full_name")})

    def _check_archived(repo_name):
        try:
            repository = _gh_json(["api", f"repos/{repo_name}"])
            return (repo_name, repository.get("archived") is True if isinstance(repository, dict) else False)
        except Exception:
            return (repo_name, False)

    repository_archived = {}
    if repo_names:
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(len(repo_names), _MAX_WORKERS)) as pool:
            for repo_name, archived in pool.map(_check_archived, repo_names):
                repository_archived[repo_name] = archived

    # Phase 2: two-pass approach. First pass collects CheckSuite items
    # (no API call needed) and PR/Issue subjects that need state checks.
    # Second pass batches all state checks in parallel.
    items = []
    subjects_to_check = []

    for notif in filtered:
        subject = notif.get("subject") or {}
        repo_info = notif.get("repository") or {}
        repo_name = repo_info.get("full_name", "")
        if repository_archived.get(repo_name):
            continue
        subject_type = subject.get("type", "")
        title = subject.get("title") or repo_name
        subject_url = subject.get("url") or ""
        latest_comment_url = subject.get("latest_comment_url") or ""

        number = None
        if subject_url:
            m = re.search(r"/(issues|pulls)/(\d+)$", subject_url)
            if m:
                number = m.group(2)

        if subject_type in {"PullRequest", "Issue"} and subject_url:
            subjects_to_check.append((notif, subject_type, title, subject_url, latest_comment_url, number, repo_name))
        elif subject_type == "CheckSuite":
            # CI failure with no PR link. Build a synthetic number from
            # the title so it de-duplicates against itself but never collides
            # with a real PR/issue number.
            hash_val = int(hashlib.md5(title.encode()).hexdigest(), 16) % (10**6)
            items.append({
                "number": f"ci-{hash_val}",
                "title": title,
                "repository": {"nameWithOwner": repo_name},
                "url": repo_info.get("html_url", ""),
                "type": "notification",
                "subject_type": subject_type,
                "notification_reason": notif.get("reason", ""),
                "notification_id": notif.get("id", ""),
                "latest_comment_url": "",
                "createdAt": notif.get("updated_at", ""),
            })

    # Phase 3: batch-fetch state for all PR/Issue subjects in parallel.
    if subjects_to_check:
        with concurrent.futures.ThreadPoolExecutor(max_workers=_MAX_PR_DETAIL_WORKERS) as pool:
            def _check_subject(s):
                notif, subject_type, title, subject_url, latest_comment_url, number, repo_name = s
                try:
                    subject_info = _gh_json(["api", subject_url])
                    state = subject_info.get("state") if isinstance(subject_info, dict) else None
                    if isinstance(state, str) and state == "open":
                        return {
                            "number": number,
                            "title": title,
                            "repository": {"nameWithOwner": repo_name},
                            "url": f"https://github.com/{repo_name}/issues/{number}" if subject_type == "Issue" else f"https://github.com/{repo_name}/pull/{number}",
                            "type": "notification",
                            "subject_type": subject_type,
                            "notification_reason": notif.get("reason", ""),
                            "notification_id": notif.get("id", ""),
                            "latest_comment_url": latest_comment_url,
                            "createdAt": notif.get("updated_at", ""),
                        }
                except Exception:
                    pass
                return None

            for item in pool.map(_check_subject, subjects_to_check):
                if item is not None:
                    items.append(item)

    return items


def _build_repo_dir_index(code_dir):
    """Map "owner/repo" (lowercased) -> the actual local directory name
    under code_dir, derived from each subdirectory's own `git remote
    origin`. Intentionally not a maintained/committed mapping -- a repo
    cloned under a shorthand directory name still resolves, with nothing
    to keep in sync. Each subdirectory's `git remote` invocation is its
    own subprocess spawn, independent of every other one, so they run
    concurrently -- a code_dir with a hundred checkouts would otherwise
    mean a hundred sequential process spawns before the dashboard can
    even render.
    """
    index = {}
    try:
        entries = [e for e in os.scandir(code_dir) if e.is_dir()]
    except OSError:
        return index
    if not entries:
        return index
    # Strip any inherited GIT_DIR/GIT_WORK_TREE/etc: if the caller's
    # environment has one set (e.g. running inside another git hook),
    git_env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}

    def _origin_for(entry):
        try:
            res = subprocess.run(
                ["git", "-C", entry.path, "remote", "get-url", "origin"],
                capture_output=True, text=True, timeout=2, env=git_env,
            )
            if res.returncode != 0:
                return None
            m = re.search(r"[:/]([^/:]+/[^/]+?)(?:\.git)?$", res.stdout.strip())
            return (m.group(1).lower(), entry.name) if m else None
        except Exception:
            return None

    with concurrent.futures.ThreadPoolExecutor(max_workers=min(len(entries), _MAX_WORKERS)) as pool:
        for result in pool.map(_origin_for, entries):
            if result:
                index[result[0]] = result[1]
    return index

def _repo_dir_index(code_dir):
    key = os.path.realpath(os.path.abspath(os.path.expanduser(code_dir)))
    with _repo_dir_indexes_lock:
        index = _repo_dir_indexes.get(key)
        if index is None:
            index = _build_repo_dir_index(code_dir)
            _repo_dir_indexes[key] = index
        return index




def _fetch_raw(config):
    default_branches = {}
    default_branch_lock = threading.Lock()
    track_authors = config.get("github", {}).get("trackAuthors", [])
    bot_review_allowlist = frozenset(
        login.casefold() for login in config.get("github", {}).get("botReviewAllowlist", [])
    )
    current_login = _get_gh_login()

    def search_prs(*filters):
        return _gh_json([
            "search", "prs", *filters, "--state=open", "--archived=false", "--limit", "50",
            "--json", "number,title,body,repository,url,isDraft,createdAt",
        ]) or []

    with concurrent.futures.ThreadPoolExecutor(
        max_workers=min(5 + len(track_authors), _MAX_WORKERS),
    ) as pool:
        review_future = pool.submit(search_prs, "--review-requested=@me")
        authored_future = pool.submit(search_prs, "--author=@me")
        tracked_futures = [
            pool.submit(search_prs, f"--author={author}") for author in track_authors
        ]
        assigned_future = pool.submit(_gh_json, [
            "search", "issues", "--assignee=@me", "--state=open", "--archived=false", "--limit", "50",
            "--json", "number,title,repository,url,createdAt",
        ])
        repo_future = pool.submit(_fetch_my_repo_issues)
        notification_future = pool.submit(_fetch_notifications)

        review_candidates = review_future.result()
        authored_candidates = authored_future.result()
        tracked_candidates = [
            (author, future.result()) for author, future in zip(track_authors, tracked_futures)
        ]
        assigned_issues = assigned_future.result() or []
        repo_issues = repo_future.result() or []
        try:
            notifications = notification_future.result() or []
        except Exception:
            notifications = []

    candidates = review_candidates + authored_candidates
    candidates.extend(pr for _, prs in tracked_candidates for pr in prs)
    unique_candidates = {}
    for pr in candidates:
        key = _pr_key(pr)
        if key is not None:
            unique_candidates.setdefault(key, pr)
    with concurrent.futures.ThreadPoolExecutor(max_workers=_MAX_PR_DETAIL_WORKERS) as detail_pool:
        detail_futures = {
            key: detail_pool.submit(
                _fetch_pr_detail,
                pr["repository"]["nameWithOwner"],
                pr["number"],
            )
            for key, pr in unique_candidates.items()
        }
        details = {key: future.result() for key, future in detail_futures.items()}

        def detail_for(pr):
            key = _pr_key(pr)
            return details.get(key) if key is not None else None

        authored_futures = [
            detail_pool.submit(
                _classify_pr_attention,
                candidate,
                current_login,
                bot_review_allowlist,
                default_branches,
                default_branch_lock,
                detail_for(candidate),
            )
            for candidate in authored_candidates
        ]
        tracked_futures = [
            (
                author,
                [
                    detail_pool.submit(
                        _classify_pr_attention,
                        candidate,
                        author,
                        bot_review_allowlist,
                        default_branches,
                        default_branch_lock,
                        detail_for(candidate),
                    )
                    for candidate in candidates
                ],
            )
            for author, candidates in tracked_candidates
        ]
        authored_prs = [
            pr for future in authored_futures
            if (pr := future.result()) is not None
        ]
        tracked_prs = []
        for author, futures in tracked_futures:
            for future in futures:
                pr = future.result()
                if pr is not None:
                    pr["tracked_author"] = author
                    tracked_prs.append(pr)

    for pr in authored_prs:
        pr["type"] = "authored_attention"
    for pr in tracked_prs:
        pr["type"] = "tracked_attention"

    def detail_for(pr):
        key = _pr_key(pr)
        return details.get(key) if key is not None else None

    review_prs = []
    for candidate in review_candidates:
        pr = dict(candidate)
        detail = detail_for(pr)
        if detail is not None:
            pr["closingIssuesReferences"] = detail.get("closingIssuesReferences") or []
            pr["isDraft"] = detail.get("isDraft", pr.get("isDraft", False))
            repo = pr["repository"]["nameWithOwner"]
            default_branch = _default_branch(
                repo, default_branches, default_branch_lock,
            ) if detail.get("baseRefName") else ""
            pr["indicators"] = _pr_indicators(
                detail, detail.get("isDraft", pr.get("isDraft", False)), default_branch,
            )
        pr["type"] = "review_request"
        review_prs.append(pr)

    for issue in assigned_issues:
        issue["type"] = "assigned_issue"
    for issue in repo_issues:
        issue["type"] = "repo_issue"
    for notification in notifications:
        notification["type"] = "notification"

    seen = set()
    combined = []
    for item in tracked_prs + review_prs + authored_prs + assigned_issues + repo_issues + notifications:
        key = _pr_key(item)
        if key is None:
            key = (item.get("repository", {}).get("nameWithOwner", ""), item.get("number"))
        if key in seen:
            continue
        seen.add(key)
        combined.append(item)
    return combined


def get_repo_from_url(url):
    # e.g., https://github.com/athal7/kb/pull/40 -> athal7/kb
    m = re.search(r"github\.com/([^/]+/[^/]+)", url)
    return m.group(1) if m else ""


def _session_prompt(gtype, reasons):
    """State-aware default message for a work session dispatched from an
    item. The action depends on both whose work it is and why it needs
    attention -- fixing my own PR is a different job from reviewing
    someone else's or nudging a teammate's.

    My PR (authored_attention), ordered by which action dominates when a
    PR carries several reasons at once:
    - Changes requested: address them (wins over everything, draft or
      not -- a requested change is a requested change).
    - Failing CI: fix the CI.
    - Merge conflict: resolve it.
    - Review comments only: respond to them.

    Not my work:
    - A PR someone asked me to review: review it.
    - A teammate's PR I track: follow up with the author (their CI to
      fix, their changes to make -- not mine).
    - An issue assigned to me or open in my repo: work on it.

    `reasons` is the attention_reasons list (empty for review requests
    and issues).
    """
    if gtype == "authored_attention":
        if "Changes Requested" in reasons:
            return "Address the requested changes."
        if "Checks Failing" in reasons:
            return "Fix the failing CI checks."
        if "Merge Conflict" in reasons:
            return "Resolve the merge conflict."
        if "Review Commented" in reasons:
            return "Respond to the review comments."
        return "Review it."
    if gtype == "tracked_attention":
        return "Follow up with the author."
    if gtype in ("assigned_issue", "repo_issue"):
        return "Work on it."
    return "Review it."


def fetch(config):
    raw = _fetch_raw(config)
    if not raw:
        return []

    code_dir = config.get("codeDir", str(Path.home() / "code"))
    # Match each repo's actual `git remote origin` against local
    # directories under code_dir, so a repo cloned under a shorthand name
    # still resolves. One-time scan, only when there's something to
    # resolve.
    repo_dir_index = _repo_dir_index(code_dir)

    items = []
    for g in raw:
        title = g.get("title", "Untitled").strip().replace("\t", " ").replace("|", "/")
        repo_name = g.get("repository", {}).get("nameWithOwner", "unknown").replace("\t", " ").replace("|", "/")
        number = str(g.get("number"))
        url = g.get("url", "")
        is_draft = g.get("isDraft", False)

        gtype = g.get("type", "")
        is_pull_request = (
            gtype in {"review_request", "authored_attention", "tracked_attention"}
            or (gtype == "notification" and g.get("subject_type") == "PullRequest")
        )
        details = ""
        if gtype == "review_request":
            weight, status = 90, "REVIEW REQUESTED"
        elif gtype == "authored_attention":
            weight, status = 88, "NEEDS ATTENTION"
            if g.get("reviewRequested"):
                status = "REVIEW REQUESTED"
        elif gtype == "tracked_attention":
            weight, status = 85, f"{g.get('tracked_author', '').upper()}: NEEDS ATTENTION"
            if g.get("reviewRequested"):
                status = f"{g.get('tracked_author', '').upper()}: REVIEW REQUESTED"
        elif gtype == "assigned_issue":
            weight, status = 75, "ASSIGNED"
        elif gtype == "notification":
            reason = g.get("notification_reason", "")
            if reason == "ci_activity":
                # ci_activity fires on both success and failure for linked PRs/issues;
                # only CheckSuite subjects (no PR/issue link) are reliably failures.
                # Use neutral status so the user can click through to check.
                weight, status = 80, "CI STATUS"
            elif reason == "mention":
                weight, status = 82, "MENTIONED"
            elif reason == "author":
                weight, status = 82, "COMMENTED"
            elif reason == "state_change":
                weight, status = 78, "SUBSCRIBED"
            else:
                weight, status = 78, "NOTIFIED"
            details = reason.replace("_", " ").title()
        else:
            # repo_issue: boost so they rank higher
            weight, status = 70, "OPEN"

        if is_draft and not is_pull_request:
            status = f"DRAFT: {status}"

        indicators = dict(g.get("indicators") or {})
        if is_pull_request and not indicators:
            indicators = {
                "ci": "—",
                "ready": "×" if is_draft else "✓",
                "review": "…" if gtype == "review_request" else "—",
                "merge": "…",
                "stacked": "—",
            }
        indicators["state"] = _status_badge(
            gtype,
            g.get("attention_reasons", []),
            is_draft,
            bool(g.get("reviewRequested")),
        )
        kind = (
            "pull_request" if is_pull_request
            else "issue" if gtype in {"assigned_issue", "repo_issue"}
            else "notification" if gtype == "notification"
            else "other"
        )

        dir_name = repo_dir_index.get(repo_name.lower(), repo_name.split("/")[-1])
        repo_path = os.path.join(code_dir, dir_name)
        slug = slugify(title)

        session_prompt = _session_prompt(gtype, g.get("attention_reasons", []))
        record = {
            "url": url,
            "number": number,
            "id": number,
            "repo_path": repo_path,
            "slug": slug,
            "repo": repo_name,
            "context": repo_name,
            "title": title,
            "status": status,
            "details": details,
            "session_prompt": session_prompt,
         }

        actions = [
            {"key": "o", "label": "open", "primary": True, "payload": {"kind": "open", "url": url}},
            {"key": "a", "label": "approve", "payload": {"kind": "approve", "id": number, "url": url}},
            {"key": "m", "label": "merge", "payload": {"kind": "merge", "id": number, "url": url}},
            {"key": "c", "label": "comment", "payload": {"kind": "comment", "id": number, "url": url}},
            {"key": "g", "label": "label", "payload": {"kind": "label", "id": number, "url": url}},
        ]
        configured_actions = config.get("github", {}).get("actions", [])
        actions.extend(resolve_configured_actions(configured_actions, record))

        items.append({
            "status": status,
            "context": repo_name,
            "title": title,
            "details": details,
            "indicators": indicators or {},
            "weight": weight,
            "id": number,
            "kind": kind,
            "created_at": g.get("createdAt", ""),
            "absorb_note": f"{details}: {title}" if details else f"{status}: {title}",
            "identity_key": f"github:{repo_name.lower()}#{number}",
            "association_keys": [
                f"github:{reference.get('repository', {}).get('nameWithOwner', repo_name).lower()}#{reference.get('number')}"
                for reference in g.get("closingIssuesReferences", [])
                if reference.get("number") is not None
            ] + _body_association_keys(g.get("body", "")),
            "actions": actions,
        })
    return items


def _confirm_and_merge(item_id, url):
    """Merge confirmation gate after the terminal UI has exited.
    It uses a plain input() y/n prompt on the bare terminal.
    """
    if not url:
        print("No URL.")
        return
    repo = get_repo_from_url(url)
    try:
        choice = input(f"\nMerge {repo}#{item_id} (squash + delete branch)? [y/N]: ").strip().lower()
    except (KeyboardInterrupt, EOFError):
        print("\nCanceled.")
        return
    if choice == "y":
        run_cmd(["gh", "pr", "merge", "--squash", "--delete-branch", item_id, "--repo", repo])
    else:
        print("Canceled.")


def act(key, payload):
    if "command" in payload:
        return run_configured_action(payload)
    kind = payload.get("kind")
    if kind == "open":
        run_cmd(["open", payload["url"]]) if payload.get("url") else print("No URL.")
    elif kind == "approve":
        url = payload.get("url")
        if not url:
            print("No URL.")
            return
        run_cmd(["gh", "pr", "review", "--approve", payload["id"], "--repo", get_repo_from_url(url)])
    elif kind == "merge":
        _confirm_and_merge(payload["id"], payload.get("url"))
    elif kind == "comment":
        url = payload.get("url")
        if not url:
            print("No URL.")
            return
        body = input("\nEnter comment body: ").strip()
        run_cmd(["gh", "issue", "comment", payload["id"], "-R", get_repo_from_url(url), "-b", body])
    elif kind == "label":
        url = payload.get("url")
        if not url:
            print("No URL.")
            return
        label = input("\nEnter label name: ").strip()
        run_cmd(["gh", "issue", "edit", payload["id"], "-R", get_repo_from_url(url), "--add-label", label])
