#!/usr/bin/env python3
"""Behavioral coverage for exact-author grouping and GitHub PR attribution."""
import copy
import importlib.machinery
import importlib.util
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "sources"))


def load_module(name, path):
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


core = load_module("attention_author_core", ROOT / "attention")
github = load_module("attention_author_github", ROOT / "sources" / "github.py")
BOT = "dependabot[bot]"


def item(**fields):
    return dict(status="REVIEW REQUESTED", context="owner/repo", title="Update dependency",
                details="", weight=90, _plugin="github", kind="pull_request", **fields)


def groups_for(match, *extra):
    return core.dashboard_groups({"dashboard": {"groups": [
        {"name": "Selected", "match": match}, *extra,
        {"name": "Other", "fallback": True},
    ]}})


def destinations(items, groups):
    return [row.rpartition("\t")[2]
            for row in core.render_grouped_dashboard_rows(items, groups)]


class AuthorGroupingTests(unittest.TestCase):
    def test_author_is_exact_and_combined_with_plugin_and_kind(self):
        groups, error = groups_for({"plugins": ["github"], "kinds": ["pull_request"],
                                    "authors": [BOT, "renovate[bot]"]})
        self.assertIsNone(error)
        candidates = [item(author=BOT), item(author="renovate[bot]"),
                      item(author="Dependabot[bot]"), item(author="app/dependabot"),
                      item(author="human"), item()]
        candidates.extend([dict(item(author=BOT), _plugin="custom"),
                           dict(item(author=BOT), kind="issue")])
        self.assertEqual(destinations(candidates, groups),
                         ["Selected", "Selected", "Other", "Other", "Other",
                          "Other", "Other", "Other"])

    def test_author_alternative_respects_outer_constraint_and_first_match(self):
        groups, error = groups_for(
            {"plugins": ["github"], "any": [{"authors": [BOT]}, {"kinds": ["issue"]}]},
            {"name": "Later", "match": {"authors": [BOT]}},
        )
        self.assertIsNone(error)
        candidates = [item(author=BOT), dict(item(author="human"), kind="issue"),
                      dict(item(author=BOT), _plugin="custom"), item(author="human")]
        self.assertEqual(destinations(candidates, groups),
                         ["Selected", "Selected", "Later", "Other"])

    def test_non_author_rule_still_matches_items_without_author(self):
        groups, error = groups_for({"plugins": ["github"], "kinds": ["pull_request"]})
        self.assertIsNone(error)
        self.assertEqual(destinations([item()], groups), ["Selected"])

    def test_invalid_author_filters_are_rejected(self):
        for authors in (BOT, [], [42], {"login": BOT}):
            with self.subTest(authors=authors):
                groups, error = groups_for({"authors": authors})
                self.assertIsNone(groups)
                self.assertTrue(error)

    def test_missing_author_does_not_match_empty_login(self):
        groups, error = groups_for({"authors": [""]})
        self.assertIsNone(error)
        self.assertEqual(destinations([item()], groups), ["Other"])

    def test_item_author_must_be_a_login_string(self):
        for author in (None, 42, {"login": BOT}, [BOT]):
            with self.subTest(author=author):
                with self.assertRaises(ValueError):
                    core.validate_and_normalize_item(item(author=author), "custom")


class GitHubAuthorTests(unittest.TestCase):
    def fetch_items(self, *, search_filter=None, search_author=None,
                    detail_author=None, cli_fallback=False, notification=False):
        candidate = {"number": 91, "title": "Update dependency", "isDraft": False,
                     "repository": {"nameWithOwner": "owner/repo"},
                     "url": "https://github.com/owner/repo/pull/91"}
        if search_author is not None:
            candidate["author"] = search_author
        detail = {"state": "OPEN", "mergeable": "CONFLICTING"}
        if detail_author is not None:
            detail["author"] = detail_author
        notice = {"id": "thread-91", "unread": True, "reason": "mention",
                  "repository": {"full_name": "owner/repo"},
                  "subject": {"type": "PullRequest", "title": "Update dependency",
                              "url": "https://api.github.com/repos/owner/repo/pulls/91"}}

        def transport(args):
            if args[:2] == ["search", "prs"]:
                return copy.deepcopy([candidate]) if search_filter in args else []
            if args[:2] == ["search", "issues"]:
                return []
            if args[:2] == ["pr", "view"]:
                return copy.deepcopy(detail)
            if args[:2] == ["api", "/notifications"]:
                return copy.deepcopy([notice]) if notification else []
            if args[:2] == ["api", "graphql"]:
                if cli_fallback:
                    return None
                if notification:
                    return {"data": {"r0": {"isArchived": False,
                                             "s0_0": copy.deepcopy(detail)}}}
                return {"data": {"r0": {"p0_0": copy.deepcopy(detail)}}}
            if args == ["api", "repos/owner/repo"]:
                return {"archived": False}
            if args == ["api", notice["subject"]["url"]]:
                return {"state": "open", "user": copy.deepcopy(detail_author)}
            raise AssertionError(f"Unexpected GitHub request: {args}")

        config = {"github": {"trackAuthors": ["tracked-account"]}}
        with (patch.object(github, "_gh_json", side_effect=transport),
              patch.object(github, "_required_gh_json", side_effect=transport),
              patch.object(github, "_get_gh_login", return_value="viewer"),
              patch.object(github, "_fetch_my_repo_issues", return_value=[]),
              patch.object(github, "_repo_dir_index", return_value={})): 
            return github.fetch(config)

    def assert_author_routes(self, items, author):
        self.assertEqual([(entry["id"], entry.get("author")) for entry in items],
                         [("91", author)])
        for entry in items:
            core.validate_and_normalize_item(entry, "github")
            entry["_plugin"] = "github"
        groups, error = groups_for({"plugins": ["github"], "kinds": ["pull_request"],
                                    "authors": [author]})
        self.assertIsNone(error)
        self.assertEqual(destinations(items, groups), ["Selected"])

    def test_review_request_uses_graphql_bot_author(self):
        self.assert_author_routes(self.fetch_items(
            search_filter="--review-requested=@me",
            search_author={"login": BOT, "type": "Bot", "is_bot": False},
            detail_author={"login": "dependabot", "__typename": "Bot"}), BOT)

    def test_review_request_recovers_author_from_cli_detail(self):
        self.assert_author_routes(self.fetch_items(
            search_filter="--review-requested=@me", cli_fallback=True,
            detail_author={"login": "app/dependabot", "is_bot": True}), BOT)

    def test_missing_detail_author_preserves_search_bot_login(self):
        self.assert_author_routes(self.fetch_items(
            search_filter="--review-requested=@me",
            search_author={"login": BOT, "type": "Bot"}), BOT)

    def test_authored_pr_preserves_human_login_case(self):
        self.assert_author_routes(self.fetch_items(
            search_filter="--author=@me",
            search_author={"login": "HumanCase"},
            detail_author={"login": "HumanCase", "__typename": "User"}), "HumanCase")

    def test_tracked_pr_uses_actual_bot_author_not_tracking_filter(self):
        self.assert_author_routes(self.fetch_items(
            search_filter="--author=tracked-account", cli_fallback=True,
            detail_author={"login": "app/renovate", "is_bot": True}), "renovate[bot]")

    def test_notification_preserves_graphql_bot_author(self):
        self.assert_author_routes(self.fetch_items(
            notification=True,
            detail_author={"login": "dependabot", "__typename": "Bot"}), BOT)

    def test_notification_preserves_rest_bot_author(self):
        self.assert_author_routes(self.fetch_items(
            notification=True, cli_fallback=True,
            detail_author={"login": BOT, "type": "Bot"}), BOT)


if __name__ == "__main__":
    unittest.main()
