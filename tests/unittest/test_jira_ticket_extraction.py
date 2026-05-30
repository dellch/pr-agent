from unittest.mock import MagicMock, patch

from pr_agent.config_loader import get_settings
from pr_agent.tools.ticket_pr_compliance_check import (
    extract_jira_tickets,
    find_jira_tickets,
)


class TestFindJiraTickets:
    """Jira key extraction from arbitrary text (PR title, description, branch name)."""

    def test_uppercase_key_with_prefix(self):
        """feature/ABC-123-description -> ABC-123"""
        assert find_jira_tickets("feature/ABC-123-description-of-branch") == ["ABC-123"]

    def test_lowercase_key_with_prefix_normalized(self):
        """bugfix/abc-123-description -> ABC-123 (case-insensitive, normalized to upper)"""
        assert find_jira_tickets("bugfix/abc-123-description-of-branch") == ["ABC-123"]

    def test_mixed_case_key_normalized(self):
        """Abc-123 -> ABC-123"""
        assert find_jira_tickets("Abc-123-fix") == ["ABC-123"]

    def test_arbitrary_prefix_segment(self):
        """Any prefix segment, not just feature/bugfix."""
        assert find_jira_tickets("chore/PROJ-45-cleanup") == ["PROJ-45"]
        assert find_jira_tickets("hotfix/proj-45-cleanup") == ["PROJ-45"]

    def test_key_at_start_no_prefix(self):
        """ABC-123-fix -> ABC-123"""
        assert find_jira_tickets("ABC-123-fix") == ["ABC-123"]

    def test_key_anywhere_in_branch(self):
        """Key embedded in the middle of a branch name is still found."""
        assert find_jira_tickets("release/v1.2.3-ABC-9-final") == ["ABC-9"]

    def test_key_in_description_text(self):
        """Key mentioned in free text."""
        assert find_jira_tickets("This implements ABC-123 as discussed") == ["ABC-123"]

    def test_browse_url(self):
        """Full Jira browse URL -> key."""
        assert find_jira_tickets(
            "see https://acme.atlassian.net/browse/ABC-123 for details"
        ) == ["ABC-123"]

    def test_no_ticket(self):
        """Branch with no key -> []"""
        assert find_jira_tickets("feature/no-ticket-here") == []
        assert find_jira_tickets("") == []

    def test_multiple_tickets_deduped_in_order(self):
        """Multiple distinct keys are all returned, de-duplicated and case-normalized."""
        result = find_jira_tickets("ABC-1 and DEF-2, again ABC-1 and abc-1")
        assert set(result) == {"ABC-1", "DEF-2"}


class TestExtractJiraTickets:
    """End-to-end extraction: find keys, fetch via the Jira client, map to ticket dicts."""

    def _configure_jira(self):
        get_settings().set("JIRA.JIRA_BASE_URL", "https://acme.atlassian.net")
        get_settings().set("JIRA.JIRA_API_EMAIL", "me@acme.com")
        get_settings().set("JIRA.JIRA_API_TOKEN", "token123")

    def _disable_jira(self):
        get_settings().set("JIRA.JIRA_BASE_URL", "")
        get_settings().set("JIRA.JIRA_API_EMAIL", "")
        get_settings().set("JIRA.JIRA_API_TOKEN", "")

    def _fake_client(self, fields=None):
        fields = fields or {"summary": "Title", "description": "Body", "labels": ["a"]}
        client = MagicMock()
        client.issue.return_value = {"fields": fields}
        return client

    def test_returns_empty_when_not_configured(self):
        self._disable_jira()
        assert extract_jira_tickets("bugfix/abc-123-x") == []

    def test_fetches_lowercase_branch_key(self):
        """The whole point: a lowercased branch key is detected and fetched as upper."""
        self._configure_jira()
        client = self._fake_client()
        with patch("pr_agent.tools.ticket_pr_compliance_check.Jira", return_value=client):
            result = extract_jira_tickets("bugfix/abc-123-description-of-branch")
        client.issue.assert_called_once_with("ABC-123")
        assert len(result) == 1
        assert result[0]["ticket_id"] == "ABC-123"
        assert result[0]["ticket_url"] == "https://acme.atlassian.net/browse/ABC-123"
        assert result[0]["title"] == "Title"
        assert result[0]["labels"] == "a"

    def test_requirements_field_populated_when_configured(self):
        """When jira_requirements_field is set, that custom field maps to requirements."""
        self._configure_jira()
        get_settings().set("JIRA.JIRA_REQUIREMENTS_FIELD", "customfield_10127")
        client = MagicMock()
        client.issue.return_value = {"fields": {
            "summary": "T", "description": "B", "labels": [],
            "customfield_10127": "Acceptance criteria text",
        }}
        with patch("pr_agent.tools.ticket_pr_compliance_check.Jira", return_value=client):
            result = extract_jira_tickets("ABC-1")
        assert result[0]["requirements"] == "Acceptance criteria text"

    def test_requirements_empty_when_field_not_configured(self):
        """With no requirements field configured, requirements stays empty."""
        self._configure_jira()
        get_settings().set("JIRA.JIRA_REQUIREMENTS_FIELD", "")
        client = self._fake_client()
        with patch("pr_agent.tools.ticket_pr_compliance_check.Jira", return_value=client):
            result = extract_jira_tickets("ABC-1")
        assert result[0]["requirements"] == ""

    def test_checks_all_tickets_when_multiple(self):
        """When several distinct keys are present, each is fetched."""
        self._configure_jira()
        client = self._fake_client()
        with patch("pr_agent.tools.ticket_pr_compliance_check.Jira", return_value=client):
            result = extract_jira_tickets("ABC-1 DEF-2 GHI-3")
        fetched = {c.args[0] for c in client.issue.call_args_list}
        assert fetched == {"ABC-1", "DEF-2", "GHI-3"}
        assert len(result) == 3

    def test_caps_candidate_keys_at_three(self):
        """No more than three candidate keys are fetched (matches the GitHub branch)."""
        self._configure_jira()
        client = self._fake_client()
        with patch("pr_agent.tools.ticket_pr_compliance_check.Jira", return_value=client):
            result = extract_jira_tickets("ABC-1 ABC-2 ABC-3 ABC-4 ABC-5")
        assert client.issue.call_count == 3
        assert len(result) == 3

    def test_skips_ticket_on_fetch_error(self):
        """A failed fetch for one key does not abort the others."""
        self._configure_jira()
        client = MagicMock()
        client.issue.side_effect = [
            Exception("404 not found"),
            {"fields": {"summary": "Second", "description": "B", "labels": []}},
        ]
        with patch("pr_agent.tools.ticket_pr_compliance_check.Jira", return_value=client):
            result = extract_jira_tickets("ABC-1 ABC-2")
        assert len(result) == 1
        assert result[0]["title"] == "Second"

    def test_nonexistent_keys_are_skipped(self):
        """Key-like noise (utf-8, sha-1) that does not resolve in Jira is skipped,
        leaving only the real ticket."""
        self._configure_jira()

        def fake_issue(key):
            if key == "ABC-123":
                return {"fields": {"summary": "Real", "description": "Body", "labels": []}}
            raise Exception("404 not found")

        client = MagicMock()
        client.issue.side_effect = fake_issue
        with patch("pr_agent.tools.ticket_pr_compliance_check.Jira", return_value=client):
            result = extract_jira_tickets("abc-123 utf-8")
        assert [t["ticket_id"] for t in result] == ["ABC-123"]
