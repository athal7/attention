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
from datetime import datetime, timedelta, timezone
import json
import os
import re
import subprocess
import threading
from pathlib import Path

from _util import resolve_configured_actions, run_cmd, run_configured_action, slugify

_MAX_WORKERS = 8
_MAX_PR_DETAIL_WORKERS = 32
_NOTIFICATION_REASONS = {
    "mention", "team_mention", "author", "comment", "manual", "subscribed",
}

_PR_DETAIL_FIELDS = (
    "mergeable,reviewDecision,statusCheckRollup,latestReviews,"
    "closingIssuesReferences,isDraft,reviewRequests,baseRefName,headRefName"
)
_PR_GRAPHQL_SELECTION = """
pullRequest(number: %d) {
  mergeable
  reviewDecision
  isDraft
  baseRefName
  headRefName
  reviewRequests(first: 50) {
    nodes {
      requestedReviewer {
        __typename
        ... on User { login }
        ... on Bot { login }
        ... on Team { name }
      }
    }
  }
  latestReviews(first: 50) {
    nodes {
      state
      submittedAt
      author { login __typename }
    }
  }
  closingIssuesReferences(first: 50) {
    nodes { number repository { nameWithOwner } }
  }
  commits(last: 1) {
    nodes { commit { committedDate } }
  }
  statusCheckRollup {
    contexts(first: 100) {
      nodes {
        ... on CheckRun { conclusion }
        ... on StatusContext { state }
      }
    }
  }
}
"""
_repo_dir_indexes = {}
_repo_dir_indexes_lock = threading.Lock()


def _pr_key(pr):
    repo = pr.get("repository", {}).get("nameWithOwner", "")
    number = pr.get("number")
    if not repo or number is None:
        return None
    return repo.casefold(), str(number)


def _normalize_pr_detail(detail):
    if not isinstance(detail, dict):
        return None

    reviews_payload = detail.get("latestReviews") or []
    reviews = (
        reviews_payload.get("nodes", [])
        if isinstance(reviews_payload, dict)
        else reviews_payload
    )
    reviews = [review for review in reviews if isinstance(review, dict)]
    bot_flags = {}
    for review in reviews:
        author = review.get("author") or {}
        login = (author.get("login") or "").casefold()
        if login and author.get("__typename") in {"Bot", "User", "Organization"}:
            bot_flags[login] = author["__typename"] == "Bot"

    check_payload = detail.get("statusCheckRollup") or []
    check_nodes = (
        check_payload.get("contexts", {}).get("nodes", [])
        if isinstance(check_payload, dict)
        else check_payload
    )
    state_to_conclusion = {
        "SUCCESS": "SUCCESS",
        "FAILURE": "FAILURE",
        "ERROR": "ERROR",
    }
    checks = []
    for node in check_nodes or []:
        if not isinstance(node, dict):
            continue
        conclusion = node.get("conclusion")
        if conclusion is None:
            conclusion = state_to_conclusion.get(node.get("state"))
        checks.append({"conclusion": conclusion})

    review_request_payload = detail.get("reviewRequests") or []
    review_request_nodes = (
        review_request_payload.get("nodes", [])
        if isinstance(review_request_payload, dict)
        else review_request_payload
    )
    closing_payload = detail.get("closingIssuesReferences") or []
    closing_nodes = (
        closing_payload.get("nodes", [])
        if isinstance(closing_payload, dict)
        else closing_payload
    )
    commit_payload = detail.get("commits") or []
    commit_nodes = (
        commit_payload.get("nodes", [])
        if isinstance(commit_payload, dict)
        else commit_payload
    )
    return {
        "mergeable": detail.get("mergeable"),
        "reviewDecision": detail.get("reviewDecision"),
        "isDraft": detail.get("isDraft", False),
        "baseRefName": detail.get("baseRefName") or "",
        "headRefName": detail.get("headRefName") or "",
        "reviewRequests": [
            node.get("requestedReviewer", node)
            for node in review_request_nodes
            if isinstance(node, dict) and node.get("requestedReviewer", node)
        ],
        "latestReviews": reviews,
        "closingIssuesReferences": [
            node for node in closing_nodes if isinstance(node, dict)
        ],
        "commits": [
            {"committedDate": node.get("commit", node).get("committedDate")}
            for node in commit_nodes
            if isinstance(node, dict) and node.get("commit", node).get("committedDate")
        ],
        "statusCheckRollup": checks,
        "_bot_flags": bot_flags,
    }


def _fetch_pr_details(prs):
    """Fetch all candidate PR details in one GraphQL request.

    GitHub CLI starts a new process for every `gh pr view` invocation. A
    single aliased GraphQL query keeps the same detail fields while avoiding
    that per-PR process and network overhead.
    """
    grouped = {}
    for pr in prs:
        key = _pr_key(pr)
        if key is None:
            continue
        try:
            number = int(pr["number"])
        except (TypeError, ValueError):
            continue
        grouped.setdefault(pr["repository"]["nameWithOwner"], []).append((key, number))
    if not grouped:
        return {}

    query = ["query {"]
    for repo_index, (repo, entries) in enumerate(grouped.items()):
        owner, _, name = repo.partition("/")
        if not owner or not name:
            continue
        query.append(
            f"r{repo_index}: repository(owner: {json.dumps(owner)}, "
            f"name: {json.dumps(name)}) {{"
        )
        for entry_index, (key, number) in enumerate(entries):
            alias = f"p{repo_index}_{entry_index}"
            query.append(f"{alias}: {_PR_GRAPHQL_SELECTION % number}")
        query.append("}")
    query.append("}")
    try:
        result = _gh_json(["api", "graphql", "-f", "query=" + "".join(query)])
    except Exception:
        return {}
    data = (result or {}).get("data") if isinstance(result, dict) else None
    if not isinstance(data, dict):
        return {}

    details = {}
    for repo_index, (repo, entries) in enumerate(grouped.items()):
        repository = data.get(f"r{repo_index}") or {}
        for entry_index, (key, _number) in enumerate(entries):
            raw = repository.get(f"p{repo_index}_{entry_index}")
            detail = _normalize_pr_detail(raw)
            if detail is not None:
                details[key] = detail
    return details


def _fetch_pr_detail(repo, number, include_commits=False):
    fields = _PR_DETAIL_FIELDS + (",commits" if include_commits else "")
    detail = _gh_json([
        "pr", "view", str(number), "-R", repo, "--json", fields,
    ])
    return detail if isinstance(detail, dict) else None


def _body_association_keys(body):
    return [
        f"linear:{identifier.upper()}"
        for identifier in re.findall(r"\b[A-Z][A-Z0-9]+-\d+\b", body or "", flags=re.IGNORECASE)
    ]


class GitHubFetchError(RuntimeError):
    """A required GitHub request did not produce complete JSON."""


def _gh_json(args, *, required=False):
    """Run `gh <args...>` and parse JSON.

    Tolerant callers receive [] on failure. Required callers raise so a
    failed request cannot replace a prior result.
    """
    try:
        res = subprocess.run(["gh"] + args, capture_output=True, text=True, timeout=30)
        if res.returncode != 0:
            raise GitHubFetchError(f"gh {' '.join(args)} exited {res.returncode}")
        return json.loads(res.stdout or "[]")
    except Exception as exc:
        if required:
            raise GitHubFetchError(f"gh {' '.join(args)} failed: {exc}") from exc
        return []


def _required_gh_json(args):
    """Call the required path while keeping direct-plugin test doubles usable."""
    try:
        return _gh_json(args, required=True)
    except TypeError:
        return _gh_json(args)


def _compose_raw(search_items, notification_items):
    """Combine the search and notification streams with search as the display winner."""
    combined = [dict(item) for item in search_items]
    by_subject = {}
    for item in combined:
        key = _pr_key(item)
        if key is not None:
            by_subject[key] = item
    for notification in notification_items:
        # Deltas also contain removals for read, filtered, or closed threads.
        if notification.get("_remove"):
            continue
        notification = dict(notification)
        notification_id = notification.get("notification_id")
        ids = [str(thread_id) for thread_id in notification.get("notification_ids", [])]
        if isinstance(notification_id, (str, int)):
            ids.insert(0, str(notification_id))
        key = _pr_key(notification)
        winner = by_subject.get(key) if key is not None else None
        if winner is None:
            if key is not None:
                by_subject[key] = notification
            winner = notification
            combined.append(winner)
        target_ids = winner.setdefault("notification_ids", [])
        for thread_id in ids:
            if thread_id not in target_ids:
                target_ids.append(thread_id)
        if target_ids and "notification_id" not in winner:
            winner["notification_id"] = target_ids[0]
    return combined


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


def _parse_github_timestamp(value):
    if not isinstance(value, str) or not value:
        return None
    try:
        timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return timestamp if timestamp.tzinfo else timestamp.replace(tzinfo=timezone.utc)


def _latest_commit_at(detail):
    timestamps = [
        timestamp
        for commit in detail.get("commits") or []
        if (timestamp := _parse_github_timestamp(commit.get("committedDate"))) is not None
    ]
    return max(timestamps, default=None)


def _review_is_superseded(review, latest_commit_at):
    submitted_at = _parse_github_timestamp(review.get("submittedAt"))
    return (
        submitted_at is not None
        and latest_commit_at is not None
        and submitted_at <= latest_commit_at
    )


def _pr_indicators(detail, is_draft):
    checks = detail.get("statusCheckRollup") or []
    failing_checks = {"FAILURE", "ERROR", "CANCELLED", "TIMED_OUT", "ACTION_REQUIRED", "STARTUP_FAILURE"}
    complete_checks = {"SUCCESS", "NEUTRAL", "SKIPPED"}
    conclusions = {check.get("conclusion") for check in checks}
    if conclusions & failing_checks:
        ci = "Failed"
    elif checks and conclusions <= complete_checks:
        ci = "Passed"
    elif checks:
        ci = "Running"
    else:
        ci = "None"

    review_states = {review.get("state") for review in detail.get("latestReviews") or []}
    if "CHANGES_REQUESTED" in review_states:
        review = "Changes"
    elif "COMMENTED" in review_states:
        review = "Commented"
    elif detail.get("reviewDecision") == "APPROVED":
        review = "Approved"
    elif detail.get("reviewRequests") or detail.get("reviewDecision") == "REVIEW_REQUIRED":
        review = "Requested"
    else:
        review = "None"

    mergeable = detail.get("mergeable")
    merge = "Conflict" if mergeable == "CONFLICTING" else "Ready" if mergeable == "MERGEABLE" else "Unknown"

    return {
        "ci": ci,
        "draft": "Yes" if is_draft else "No",
        "review": review,
        "merge": merge,
        "target": detail.get("baseRefName") or "—",
    }


def _apply_pr_detail(pr, detail):
    if detail is None:
        return
    pr["reviewRequested"] = bool(detail.get("reviewRequests"))
    pr["closingIssuesReferences"] = detail.get("closingIssuesReferences") or []
    pr["isDraft"] = detail.get("isDraft", pr.get("isDraft", False))
    pr["baseRefName"] = detail.get("baseRefName") or ""
    pr["headRefName"] = detail.get("headRefName") or ""
    pr["indicators"] = _pr_indicators(detail, pr["isDraft"])

def _status_label(gtype, reasons, review_requested, is_draft):
    if is_draft:
        return "Draft"
    if "Merge Conflict" in reasons:
        return "Merge conflict"
    if "Changes Requested" in reasons:
        return "Changes requested"
    if "Checks Failing" in reasons:
        return "CI failing"
    if "Review Commented" in reasons:
        return "Reply needed"
    if gtype == "review_request" or review_requested:
        return "Review requested"
    if gtype == "assigned_issue":
        return "Assigned"
    if gtype == "repo_issue":
        return "Triage"
    if gtype == "notification":
        return "Reply needed"
    return "Ready"
def _classify_pr_attention(pr, expected_author, bot_review_allowlist, detail):
    key = _pr_key(pr)
    if key is None or detail is None:
        return None
    repo, number = pr["repository"]["nameWithOwner"], pr["number"]
    reasons = []
    latest_review_states = set()
    bot_flags = detail.get("_bot_flags")
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
    _apply_pr_detail(pr, detail)
    pr["attention_reasons"] = reasons
    return pr


def _should_suppress_bot_review_notification(
    detail, repo, number, expected_author, bot_review_allowlist,
):
    latest_commit_at = _latest_commit_at(detail)
    bot_flags = detail.get("_bot_flags")
    if bot_flags is None:
        bot_flags = _fetch_review_bot_flags(repo, number)
    suppress = False
    for review in detail.get("latestReviews") or []:
        if review.get("state") != "COMMENTED":
            continue
        reviewer = review.get("author", {}).get("login") or ""
        if not reviewer or reviewer.casefold() == expected_author.casefold():
            continue
        reviewer_cf = reviewer.casefold()
        is_bot = bot_flags.get(reviewer_cf)
        if is_bot is None:
            is_bot = _is_bot_login(reviewer)
        if not is_bot:
            return False
        canonical = reviewer_cf if reviewer_cf.endswith("[bot]") else f"{reviewer_cf}[bot]"
        if canonical in bot_review_allowlist or reviewer_cf in bot_review_allowlist:
            if not _review_is_superseded(review, latest_commit_at):
                return False
        suppress = True
    return suppress



def _fetch_pr_attention(
    author, detail_pool, bot_review_allowlist=frozenset(),
    current_login=None, detail_lookup=None,
):
    prs = _gh_json([
        "search", "prs", f"--author={author}", "--state=open", "--archived=false", "--limit", "50",
        "--json", "number,title,body,repository,url,isDraft,createdAt",
    ])
    if not prs:
        return []
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
            pr, expected_author, bot_review_allowlist, detail_for(pr),
        )) is not None
    ]


def _fetch_my_repo_issues(required=False):
    """Return only unassigned issues in owned repositories for triage."""
    request = [
        "search", "issues", "--owner=@me", "--state=open", "--archived=false", "--limit", "50",
        "--json", "number,title,repository,url,createdAt,assignees",
    ]
    issues = _required_gh_json(request) if required else _gh_json(request)
    return [issue for issue in issues or [] if not issue.get("assignees")]


def _fetch_notification_state(notifications):
    """Fetch notification repository and subject state in one GraphQL query."""
    targets = {}
    for notif in notifications:
        subject = notif.get("subject") or {}
        repo = (notif.get("repository") or {}).get("full_name", "")
        if not repo:
            continue
        repo_targets = targets.setdefault(repo, set())
        subject_url = subject.get("url") or ""
        match = re.search(r"/(issues|pulls)/(\d+)$", subject_url)
        if subject.get("type") not in {"Issue", "PullRequest"} or not match:
            continue
        subject_type = "PullRequest" if match.group(1) == "pulls" else "Issue"
        repo_targets.add((subject_type, int(match.group(2))))
    if not targets:
        return {}, {}

    query = ["query {"]
    target_aliases = {}
    for repo_index, (repo, repo_targets) in enumerate(targets.items()):
        owner, _, name = repo.partition("/")
        if not owner or not name:
            return None
        query.append(
            f"r{repo_index}: repository(owner: {json.dumps(owner)}, "
            f"name: {json.dumps(name)}) {{ isArchived "
        )
        for target_index, (subject_type, number) in enumerate(sorted(repo_targets)):
            alias = f"s{repo_index}_{target_index}"
            field = "pullRequest" if subject_type == "PullRequest" else "issue"
            query.append(
                f"{alias}: {field}(number: {number}) {{ state "
                + ("isDraft baseRefName headRefName " if subject_type == "PullRequest" else "")
                + "}"
            )
            target_aliases[(repo, subject_type, number)] = alias
        query.append("}")
    query.append("}")
    try:
        result = _gh_json(["api", "graphql", "-f", "query=" + "".join(query)])
    except Exception:
        return None
    data = (result or {}).get("data") if isinstance(result, dict) else None
    if not isinstance(data, dict):
        return None

    repository_archived = {}
    subject_state = {}
    for repo_index, (repo, repo_targets) in enumerate(targets.items()):
        repository = data.get(f"r{repo_index}")
        if not isinstance(repository, dict):
            return None
        repository_archived[repo] = repository.get("isArchived") is True
        for subject_type, number in sorted(repo_targets):
            alias = target_aliases[(repo, subject_type, number)]
            raw = repository.get(alias)
            if not isinstance(raw, dict):
                return None
            base_ref = raw.get("baseRefName") or ""
            head_ref = raw.get("headRefName") or ""
            raw_state = raw.get("state")
            subject_state[(repo, subject_type, number)] = {
                "state": raw_state.lower() if isinstance(raw_state, str) else raw_state,
                "draft": raw.get("isDraft"),
                "base": {"ref": base_ref} if base_ref else {},
                "head": {"ref": head_ref} if head_ref else {},
            }
    return repository_archived, subject_state


def _notification_state_key(notif):
    subject = notif.get("subject") or {}
    repo = (notif.get("repository") or {}).get("full_name", "")
    match = re.search(r"/(issues|pulls)/(\d+)$", subject.get("url") or "")
    if not repo or not match:
        return None
    subject_type = "PullRequest" if match.group(1) == "pulls" else "Issue"
    return repo, subject_type, int(match.group(2))

def _notification_browser_url(notification, subject_info):
    subject = notification.get("subject") or {}
    repository = notification.get("repository") or {}
    return (
        subject_info.get("html_url", "") if isinstance(subject_info, dict) else ""
    ) or repository.get("html_url", "")


def _notification_item(notification, subject_state=None, subject_info=None):
    subject = notification.get("subject") or {}
    repository = notification.get("repository") or {}
    thread_id = str(notification.get("id", ""))
    repo_name = repository.get("full_name", "")
    subject_type = subject.get("type", "")
    subject_url = subject.get("url") or ""
    match = re.search(r"/(issues|pulls)/(\d+)$", subject_url)
    if subject_type in {"Issue", "PullRequest"} and match:
        number = match.group(2)
        url = (
            f"https://github.com/{repo_name}/issues/{number}"
            if subject_type == "Issue"
            else f"https://github.com/{repo_name}/pull/{number}"
        )
    else:
        number = f"notification-{thread_id}"
        url = _notification_browser_url(notification, subject_info)
    item = {
        "number": number,
        "title": subject.get("title") or repo_name,
        "repository": {"nameWithOwner": repo_name},
        "url": url,
        "type": "notification",
        "subject_type": subject_type,
        "notification_reason": notification.get("reason", ""),
        "notification_id": thread_id,
        "latest_comment_url": subject.get("latest_comment_url") or "",
        "createdAt": notification.get("updated_at", ""),
    }
    if subject_type == "PullRequest" and isinstance(subject_state, dict):
        item["isDraft"] = bool(subject_state.get("draft"))
        item["baseRefName"] = (subject_state.get("base") or {}).get("ref", "")
        item["headRefName"] = (subject_state.get("head") or {}).get("ref", "")
    return item


def _fetch_notification_delta(
    current_login=None, bot_review_allowlist=frozenset(), since=None,
):
    """Fetch and validate a durable delta of unread notification threads."""
    poll_started = datetime.now(timezone.utc)
    args = ["api", "/notifications", "--method", "GET", "--paginate", "-f", "per_page=50"]
    if since:
        cursor = _parse_github_timestamp(since)
        if cursor is None:
            raise GitHubFetchError("invalid notification cursor")
        replay_from = cursor - timedelta(seconds=60)
        args.extend(["-f", f"since={replay_from.isoformat().replace('+00:00', 'Z')}"])
    notifications = _required_gh_json(args)
    if not isinstance(notifications, list):
        raise GitHubFetchError("notification response was not a list")

    outcomes = {}
    accepted = []
    for notification in notifications:
        if not isinstance(notification, dict):
            continue
        thread_id = str(notification.get("id", ""))
        if not thread_id:
            continue
        subject = notification.get("subject") or {}
        repository = notification.get("repository") or {}
        if (
            not notification.get("unread")
            or notification.get("reason") not in _NOTIFICATION_REASONS
            or not isinstance(subject, dict)
            or not isinstance(repository, dict)
            or not repository.get("full_name")
        ):
            outcomes[thread_id] = {"notification_id": thread_id, "_remove": True}
            continue
        accepted.append(notification)

    batched_state = _fetch_notification_state(accepted)
    if batched_state is None:
        repository_archived = {}
        repo_names = sorted({
            notification["repository"]["full_name"] for notification in accepted
        })

        def fetch_repository_state(repo_name):
            try:
                repository = _gh_json(["api", f"repos/{repo_name}"])
            except Exception:
                return repo_name, True
            return (
                repo_name,
                repository.get("archived") is True if isinstance(repository, dict) else True,
            )

        with concurrent.futures.ThreadPoolExecutor(
            max_workers=min(len(repo_names), _MAX_WORKERS) or 1,
        ) as pool:
            for repo_name, archived in pool.map(fetch_repository_state, repo_names):
                repository_archived[repo_name] = archived
        subject_state = {}
    else:
        repository_archived, subject_state = batched_state
        unresolved_repositories = sorted({
            notification["repository"]["full_name"]
            for notification in accepted
            if notification["repository"]["full_name"] not in repository_archived
        })

        def fetch_unresolved_repository(repo_name):
            try:
                repository = _gh_json(["api", f"repos/{repo_name}"])
            except Exception:
                return repo_name, True
            return (
                repo_name,
                repository.get("archived") is True if isinstance(repository, dict) else True,
            )

        if unresolved_repositories:
            with concurrent.futures.ThreadPoolExecutor(
                max_workers=min(len(unresolved_repositories), _MAX_WORKERS),
            ) as pool:
                for repo_name, archived in pool.map(
                    fetch_unresolved_repository, unresolved_repositories,
                ):
                    repository_archived[repo_name] = archived

    def validate_notification(notification):
        thread_id = str(notification["id"])
        subject = notification["subject"]
        repo_name = notification["repository"]["full_name"]
        if repository_archived.get(repo_name, True):
            return {"notification_id": thread_id, "_remove": True}
        subject_type = subject.get("type", "")
        state_key = _notification_state_key(notification)
        state = subject_state.get(state_key) if state_key is not None else None
        subject_info = None
        if subject_type in {"Issue", "PullRequest"}:
            if state is None:
                subject_info = _gh_json(["api", subject.get("url", "")])
                state = subject_info if isinstance(subject_info, dict) else {}
            if state.get("state") != "open":
                return {"notification_id": thread_id, "_remove": True}
            if (
                subject_type == "PullRequest"
                and not subject.get("latest_comment_url")
                and notification.get("reason") == "author"
            ):
                number = _notification_state_key(notification)[2]
                detail = _fetch_pr_detail(repo_name, number, include_commits=True)
                if (
                    detail is not None
                    and _should_suppress_bot_review_notification(
                        detail, repo_name, number, current_login or "",
                        bot_review_allowlist,
                    )
                ):
                    return {"notification_id": thread_id, "_remove": True}
            return _notification_item(notification, state)
        subject_url = subject.get("url") or ""
        if subject_url:
            subject_info = _gh_json(["api", subject_url])
        return _notification_item(notification, subject_info=subject_info)

    with concurrent.futures.ThreadPoolExecutor(max_workers=_MAX_WORKERS) as pool:
        for result in pool.map(validate_notification, accepted):
            outcomes[str(result["notification_id"])] = result
    return (
        [
            outcomes[str(notification["id"])]
            for notification in notifications
            if isinstance(notification, dict)
            and str(notification.get("id", ""))
            and str(notification["id"]) in outcomes
        ],
        poll_started.isoformat().replace("+00:00", "Z"),
    )
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




def _fetch_search_items(config, current_login=None, bot_review_allowlist=None):
    github = config.get("github", {}) if isinstance(config, dict) else {}
    track_authors = github.get("trackAuthors", [])
    if not isinstance(track_authors, list):
        track_authors = []
    if bot_review_allowlist is None:
        bot_review_allowlist = frozenset(
            login.casefold()
            for login in github.get("botReviewAllowlist", [])
            if isinstance(login, str)
        )
    if current_login is None:
        current_login = _get_gh_login()

    def search_prs(*filters):
        return _required_gh_json([
            "search", "prs", *filters, "--state=open", "--archived=false", "--limit", "50",
            "--json", "number,title,body,repository,url,isDraft,createdAt",
        ]) or []

    def fetch_repo_issues():
        try:
            return _fetch_my_repo_issues(required=True)
        except TypeError:
            return _fetch_my_repo_issues()

    with concurrent.futures.ThreadPoolExecutor(
        max_workers=min(5 + len(track_authors), _MAX_WORKERS),
    ) as pool:
        review_future = pool.submit(search_prs, "--review-requested=@me")
        authored_future = pool.submit(search_prs, "--author=@me")
        tracked_futures = [
            pool.submit(search_prs, f"--author={author}") for author in track_authors
        ]
        assigned_future = pool.submit(_required_gh_json, [
            "search", "issues", "--assignee=@me", "--state=open", "--archived=false", "--limit", "50",
            "--json", "number,title,repository,url,createdAt",
        ])
        repo_future = pool.submit(fetch_repo_issues)

        review_candidates = review_future.result()
        authored_candidates = authored_future.result()
        tracked_candidates = [
            (author, future.result()) for author, future in zip(track_authors, tracked_futures)
        ]
        assigned_issues = assigned_future.result() or []
        repo_issues = repo_future.result() or []

    candidates = review_candidates + authored_candidates
    candidates.extend(pr for _, prs in tracked_candidates for pr in prs)
    unique_candidates = {}
    for pr in candidates:
        key = _pr_key(pr)
        if key is not None:
            unique_candidates.setdefault(key, pr)
    with concurrent.futures.ThreadPoolExecutor(max_workers=_MAX_PR_DETAIL_WORKERS) as detail_pool:
        details = _fetch_pr_details(list(unique_candidates.values()))
        missing = [
            (key, pr) for key, pr in unique_candidates.items()
            if key not in details
        ]
        if missing:
            detail_futures = {
                key: detail_pool.submit(
                    _fetch_pr_detail,
                    pr["repository"]["nameWithOwner"],
                    pr["number"],
                )
                for key, pr in missing
            }
            details.update({
                key: future.result() for key, future in detail_futures.items()
            })

        def detail_for(pr):
            key = _pr_key(pr)
            return details.get(key) if key is not None else None

        authored_futures = [
            detail_pool.submit(
                _classify_pr_attention,
                candidate,
                current_login,
                bot_review_allowlist,
                detail_for(candidate),
            )
            for candidate in authored_candidates
        ]
        tracked_detail_futures = [
            (
                author,
                [
                    detail_pool.submit(
                        _classify_pr_attention,
                        candidate,
                        author,
                        bot_review_allowlist,
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
        for author, futures in tracked_detail_futures:
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
        _apply_pr_detail(pr, detail_for(pr))
        pr["type"] = "review_request"
        review_prs.append(pr)

    for issue in assigned_issues:
        issue["type"] = "assigned_issue"
    for issue in repo_issues:
        issue["type"] = "repo_issue"

    seen = set()
    combined = []
    for item in tracked_prs + review_prs + authored_prs + assigned_issues + repo_issues:
        key = _pr_key(item)
        if key is None:
            key = (item.get("repository", {}).get("nameWithOwner", ""), item.get("number"))
        if key in seen:
            continue
        seen.add(key)
        combined.append(item)
    return combined


def _fetch_raw(config):
    """Live retrieval seam retained for focused tests and manual refreshes."""
    github = config.get("github", {}) if isinstance(config, dict) else {}
    allowlist = frozenset(
        login.casefold()
        for login in github.get("botReviewAllowlist", [])
        if isinstance(login, str)
    )
    current_login = _get_gh_login()
    search_items = _fetch_search_items(config, current_login, allowlist)
    notification_items, _ = _fetch_notification_delta(current_login, allowlist)
    return _compose_raw(search_items, notification_items)


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
                "ci": "None",
                "draft": "Yes" if is_draft else "No",
                "review": "Requested" if gtype == "review_request" else "None",
                "merge": "Unknown",
                "target": g.get("baseRefName") or "—",
            }
        indicators["state"] = _status_label(
            gtype,
            g.get("attention_reasons", []),
            bool(g.get("reviewRequested")),
            is_draft,
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
        notification_ids = [str(thread_id) for thread_id in g.get("notification_ids", [])]
        notification_id = str(g.get("notification_id", ""))
        if notification_id and notification_id not in notification_ids:
            notification_ids.insert(0, notification_id)
        if notification_ids:
            acknowledgement = {
                "notification_ids": notification_ids,
            }
            for action in actions:
                action["payload"].update(acknowledgement)
            used_keys = {action["key"].lower() for action in actions}
            dismiss_key = "d" if "d" not in used_keys else next(
                str(digit) for digit in range(1, 10)
                if str(digit) not in used_keys
            )
            actions.append({
                "key": dismiss_key,
                "label": "dismiss",
                "payload": {"kind": "dismiss_notification", **acknowledgement},
            })

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
            "base_branch": g.get("baseRefName", ""),
            "head_branch": g.get("headRefName", ""),
            "association_keys": [
                f"github:{reference.get('repository', {}).get('nameWithOwner', repo_name).lower()}#{reference.get('number')}"
                for reference in g.get("closingIssuesReferences", [])
                if reference.get("number") is not None
            ] + _body_association_keys(g.get("body", "")),
            "actions": actions,
        })
    branch_owners = {
        (item["context"].casefold(), item["head_branch"]): item["identity_key"]
        for item in items
        if item.get("kind") == "pull_request" and item.get("head_branch")
    }
    for item in items:
        if item.get("kind") != "pull_request" or not item.get("base_branch"):
            continue
        parent = branch_owners.get((item["context"].casefold(), item["base_branch"]))
        if parent and parent != item["identity_key"]:
            item["parent_identity_key"] = parent
    return items


def _confirm_and_merge(item_id, url):
    """Run the merge only after explicit terminal confirmation."""
    if not url:
        print("No URL.")
        return False
    repo = get_repo_from_url(url)
    try:
        choice = input(f"\nMerge {repo}#{item_id} (squash + delete branch)? [y/N]: ").strip().lower()
    except (KeyboardInterrupt, EOFError):
        print("\nCanceled.")
        return False
    if choice != "y":
        print("Canceled.")
        return False
    return run_cmd(
        ["gh", "pr", "merge", "--squash", "--delete-branch", item_id, "--repo", repo]
    )


def _mark_notification_threads_read(thread_ids):
    marked = set()
    for thread_id in thread_ids:
        thread_id = str(thread_id)
        if thread_id.isdigit() and run_cmd([
            "gh", "api", "--method", "PATCH", f"/notifications/threads/{thread_id}",
        ]):
            marked.add(thread_id)
    return marked


def _acknowledge_notifications(payload):
    return bool(_mark_notification_threads_read(payload.get("notification_ids", [])))


def act(key, payload):
    if payload.get("kind") == "dismiss_notification":
        return _acknowledge_notifications(payload)
    if "command" in payload:
        succeeded = run_configured_action(payload)
    else:
        kind = payload.get("kind")
        if kind == "open":
            if not payload.get("url"):
                print("No URL.")
                return False
            succeeded = run_cmd(["open", payload["url"]])
        elif kind == "approve":
            url = payload.get("url")
            if not url:
                print("No URL.")
                return False
            succeeded = run_cmd([
                "gh", "pr", "review", "--approve", payload["id"],
                "--repo", get_repo_from_url(url),
            ])
        elif kind == "merge":
            succeeded = _confirm_and_merge(payload["id"], payload.get("url"))
        elif kind == "comment":
            url = payload.get("url")
            if not url:
                print("No URL.")
                return False
            try:
                body = input("\nEnter comment body: ").strip()
            except (KeyboardInterrupt, EOFError):
                print("\nCanceled.")
                return False
            succeeded = run_cmd([
                "gh", "issue", "comment", payload["id"], "-R",
                get_repo_from_url(url), "-b", body,
            ])
        elif kind == "label":
            url = payload.get("url")
            if not url:
                print("No URL.")
                return False
            try:
                label = input("\nEnter label name: ").strip()
            except (KeyboardInterrupt, EOFError):
                print("\nCanceled.")
                return False
            succeeded = run_cmd([
                "gh", "issue", "edit", payload["id"], "-R",
                get_repo_from_url(url), "--add-label", label,
            ])
        else:
            return False
    if succeeded:
        _acknowledge_notifications(payload)
    return succeeded
