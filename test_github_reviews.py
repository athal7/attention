#!/usr/bin/env python3
"""Requested-review visibility across GitHub detail and identity failures."""
import copy
import importlib.util
import json
from pathlib import Path
import re
import subprocess
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "sources"))
spec = importlib.util.spec_from_file_location("github_reviews", ROOT / "sources" / "github.py")
github = importlib.util.module_from_spec(spec)
spec.loader.exec_module(github)


def candidate(number, repo="owner/repo"):
    return {"number": number, "repository": {"nameWithOwner": repo},
            "title": f"Review {number}", "url": f"https://github.com/{repo}/pull/{number}"}


def detail(author=None, state="COMMENTED", assignees=None):
    return {"state": "OPEN", "assignees": assignees or [],
            "reviewRequests": [{"login": "viewer"}],
            "latestReviews": [{"author": author, "state": state}] if author else []}


def graphql_response(args, details):
    """Resolve aliased PR selections against repository/number fixture keys."""
    data = {}
    repository = None
    for match in re.finditer(
        r'(r\d+): repository\(owner: ("[^"]*"), name: ("[^"]*")\)'
        r'|(p\d+_\d+):\s*pullRequest\(number: (\d+)\)', args[-1],
    ):
        repo_alias, owner, name, pr_alias, number = match.groups()
        if repo_alias:
            repository = f"{json.loads(owner)}/{json.loads(name)}"
            repo_data = data.setdefault(repo_alias, {})
        else:
            repo_data[pr_alias] = copy.deepcopy(details.get((repository, int(number))))
    return {"data": data}


class RequestedReviewTests(unittest.TestCase):
    def collect(self, prs, transport):
        with (patch.object(github, "_gh_json", side_effect=transport),
              patch.object(github, "_get_gh_login", return_value="viewer"),
              patch.object(github, "_fetch_my_repo_issues", return_value=[]),
              patch.object(github, "_fetch_notification_delta", return_value=([], None)),
              patch.object(github, "_repo_dir_index", return_value={})):
            return github.fetch({})

    def search_transport(self, prs, fetch_detail):
        def transport(args, **kwargs):
            if args[:2] == ["search", "prs"]:
                return copy.deepcopy(prs) if "--review-requested=@me" in args else []
            if args[:2] == ["search", "issues"]:
                return []
            return fetch_detail(args)
        return transport

    def test_http_504_then_metadata_less_copilot_remains_visible(self):
        pr = candidate(1337, "0din-ai/odin")
        fallback = detail({"login": "copilot-pull-request-reviewer"})
        transport = self.search_transport([pr], lambda args: (
            fallback if args[:2] == ["pr", "view"] else None
        ))

        def run(command, **kwargs):
            args = command[1:]
            if args[:2] == ["api", "graphql"]:
                return subprocess.CompletedProcess(command, 1, "", "HTTP 504")
            return subprocess.CompletedProcess(command, 0, json.dumps(transport(args)), "")

        with (patch.object(github.subprocess, "run", side_effect=run),
              patch.object(github, "_get_gh_login", return_value="viewer"),
              patch.object(github, "_fetch_my_repo_issues", return_value=[]),
              patch.object(github, "_fetch_notification_delta", return_value=([], None)),
              patch.object(github, "_repo_dir_index", return_value={})):
            items = github.fetch({})
        self.assertEqual([(item["id"], item["status"], item["indicators"]["state"])
                          for item in items], [("1337", "REVIEW REQUESTED", "Review requested")])

    def test_only_confirmed_other_human_submitted_reviews_hide_requests(self):
        cases = [
            ({"login": "copilot-pull-request-reviewer"}, True),
            ({"login": "unknown-reviewer"}, True),
            ({"login": "bot", "__typename": "Bot"}, True),
            ({"login": "bot", "type": "Bot"}, True),
            ({"login": "bot", "is_bot": True}, True),
            ({"login": "app[bot]", "is_bot": False}, True),
            ({"login": "bot", "__typename": "Bot", "is_bot": False}, True),
            ({"login": "Viewer", "__typename": "User"}, True),
            ({"login": "human", "__typename": "User"}, False),
            ({"login": "human", "type": "User"}, False),
            ({"login": "human", "is_bot": False}, False),
        ]
        for author, visible in cases:
            for state in ("APPROVED", "CHANGES_REQUESTED", "COMMENTED", "PENDING", "DISMISSED"):
                with self.subTest(author=author, state=state):
                    raw = detail(author, state)
                    transport = self.search_transport([candidate(1)], lambda args: (
                        [] if args[:2] == ["api", "graphql"] else raw
                    ))
                    items = self.collect([candidate(1)], transport)
                    expected = ["1"] if visible or state in {"PENDING", "DISMISSED"} else []
                    self.assertEqual([item["id"] for item in items], expected)

    def test_assignee_policy_is_independent_of_unknown_reviewer_identity(self):
        for assignees, visible in (([], True), ([{"login": "VIEWER"}], True),
                                   ([{"login": "other"}], False),
                                   ([{"login": "viewer"}, {"login": "other"}], False)):
            with self.subTest(assignees=assignees):
                raw = detail({"login": "copilot-pull-request-reviewer"}, assignees=assignees)
                transport = self.search_transport([candidate(1)], lambda args: (
                    [] if args[:2] == ["api", "graphql"] else raw
                ))
                self.assertEqual([item["id"] for item in self.collect([candidate(1)], transport)],
                                 ["1"] if visible else [])

    def test_detail_failure_keeps_request_visible(self):
        for batch in (None, [], {}, {"data": None}, {"data": {"r0": None}}):
            for fallback in (None, []):
                with self.subTest(batch=batch, fallback=fallback):
                    transport = self.search_transport([candidate(1)], lambda args: (
                        batch if args[:2] == ["api", "graphql"] else fallback
                    ))
                    self.assertEqual([item["id"] for item in self.collect([candidate(1)], transport)], ["1"])

    def test_batches_preserve_all_candidates_across_repositories_and_boundaries(self):
        limit = github._PR_DETAIL_BATCH_SIZE
        for count in (0, 1, limit, limit + 1, 2 * limit, 2 * limit + 1, 59):
            with self.subTest(count=count):
                prs = [candidate(n, f"owner/repo{n % 3}") for n in range(count)]
                details = {(pr["repository"]["nameWithOwner"], pr["number"]):
                           {"headRefOid": f"head-{pr['number']}"} for pr in prs}
                sizes = []

                def transport(args):
                    sizes.append(len(re.findall(r"pullRequest\(number:", args[-1])))
                    return graphql_response(args, details)

                with patch.object(github, "_gh_json", side_effect=transport):
                    result = github._fetch_pr_details(prs)
                self.assertEqual({key: value["headRefOid"] for key, value in result.items()},
                                 {(repo, str(n)): raw["headRefOid"] for (repo, n), raw in details.items()})
                self.assertEqual(sizes, [min(limit, count - start) for start in range(0, count, limit)])

    def test_failed_middle_batch_and_partial_results_fall_back_independently(self):
        limit = github._PR_DETAIL_BATCH_SIZE
        prs = [candidate(n) for n in range(2 * limit + 1)]
        details = {("owner/repo", n): detail({"login": "bot", "__typename": "Bot"})
                   for n in range(len(prs))}
        # A confirmed human in the successful final batch must still hide its request.
        details[("owner/repo", 2 * limit)] = detail({"login": "human", "__typename": "User"})
        del details[("owner/repo", 0)]
        batches = []
        fallbacks = []

        def fetch_detail(args):
            if args[:2] == ["api", "graphql"]:
                batches.append(args[-1])
                if len(batches) == 2:
                    raise subprocess.TimeoutExpired("gh", 30)
                return graphql_response(args, details)
            if args[:2] == ["pr", "view"]:
                fallbacks.append(int(args[2]))
                return detail({"login": "copilot-pull-request-reviewer"})
            raise AssertionError(args)

        items = self.collect(prs, self.search_transport(prs, fetch_detail))
        self.assertEqual([item["id"] for item in items], [str(n) for n in range(2 * limit)])
        self.assertEqual(sorted(fallbacks), [0, *range(limit, 2 * limit)])


class NotificationReuseTests(unittest.TestCase):
    def test_overlapping_notification_needs_no_second_graphql_request(self):
        pr = candidate(1)
        notification = {
            "id": "thread-1", "unread": True, "reason": "mention",
            "repository": {"full_name": "owner/repo"},
            "subject": {"type": "PullRequest", "url": "https://api.github.com/repos/owner/repo/pulls/1"},
        }
        queries = []

        def transport(args, **kwargs):
            if args[:2] == ["search", "prs"]:
                return [copy.deepcopy(pr)] if "--review-requested=@me" in args else []
            if args[:2] == ["search", "issues"]:
                return []
            if args[:2] == ["api", "/notifications"]:
                return [notification]
            if args[:2] == ["api", "graphql"]:
                queries.append(args[-1])
                return {"data": {"r0": {"isArchived": False, "p0_0": {
                    "state": "OPEN", "isDraft": False, "baseRefName": "main",
                    "headRefName": "feature", "author": {"login": "author"},
                }}}}
            raise AssertionError(args)

        with (patch.object(github, "_gh_json", side_effect=transport),
              patch.object(github, "_get_gh_login", return_value="viewer"),
              patch.object(github, "_fetch_my_repo_issues", return_value=[])):
            items = github._fetch_raw({})
        self.assertEqual(len(queries), 1)
        self.assertEqual([(item["number"], item["notification_ids"]) for item in items],
                         [(1, ["thread-1"])])
        self.assertNotIn("_notification_state", items[0])

    def test_incomplete_search_detail_falls_back_to_notification_query(self):
        notification = {
            "id": "thread-1", "unread": True, "reason": "mention",
            "repository": {"full_name": "owner/repo"},
            "subject": {"type": "PullRequest", "url": "https://api.github.com/repos/owner/repo/pulls/1"},
        }
        pr = candidate(1)
        github._apply_pr_detail(pr, {"state": "OPEN", "isDraft": False})

        def transport(args, **kwargs):
            if args[:2] == ["api", "/notifications"]:
                return [notification]
            if args[:2] == ["api", "graphql"]:
                return {"data": {"r0": {"isArchived": False, "s0_0": {
                    "state": "CLOSED",
                }}}}
            raise AssertionError(args)

        with patch.object(github, "_gh_json", side_effect=transport):
            items, _ = github._fetch_notification_delta(search_items=[pr])
        self.assertEqual(items, [{"notification_id": "thread-1", "_remove": True}])

    def test_mixed_subjects_and_archived_repository_preserve_filtering(self):
        cached = {"number": 1, "repository": {"nameWithOwner": "owner/repo"},
                  "_notification_state": {"archived": True, "state": "open", "draft": False,
                                          "base": {}, "head": {}, "author": {}}}
        def notice(number):
            return {"id": str(number), "unread": True, "reason": "mention",
                    "repository": {"full_name": "owner/repo"},
                    "subject": {"type": "PullRequest",
                                "url": f"https://api.github.com/repos/owner/repo/pulls/{number}"}}

        def transport(args, **kwargs):
            if args[:2] == ["api", "/notifications"]:
                return [notice(1), notice(2)]
            if args[:2] == ["api", "graphql"]:
                return {"data": {"r0": {"isArchived": True, "s0_0": {"state": "OPEN"}}}}
            raise AssertionError(args)

        with patch.object(github, "_gh_json", side_effect=transport):
            items, _ = github._fetch_notification_delta(search_items=[cached])
        self.assertEqual([item["notification_id"] for item in items if item.get("_remove")], ["1", "2"])


if __name__ == "__main__":
    unittest.main()
