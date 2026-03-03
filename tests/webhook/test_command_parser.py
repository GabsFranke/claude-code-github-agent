"""Unit tests for command parser."""

from services.webhook.parsers.command_parser import REGISTERED_COMMANDS, parse_command


class TestParseCommand:
    """Test command parsing from comment body."""

    def test_parse_agent_command_with_query(self):
        """Test parsing /agent command with query."""
        comment = "/agent please review this code"
        result = parse_command(comment)
        assert result == "please review this code"

    def test_parse_agent_command_multiline(self):
        """Test parsing /agent command in multiline comment."""
        comment = """
        Some text here
        /agent analyze the performance
        More text
        """
        result = parse_command(comment)
        assert result == "analyze the performance"

    def test_parse_registered_command_review_pr(self):
        """Test parsing registered /review-pr command."""
        comment = "/review-pr"
        result = parse_command(comment)
        assert result == "review-pr"

    def test_parse_registered_command_pr_review(self):
        """Test parsing registered /pr-review command."""
        comment = "/pr-review"
        result = parse_command(comment)
        assert result == "pr-review"

    def test_parse_registered_command_review(self):
        """Test parsing registered /review command."""
        comment = "/review"
        result = parse_command(comment)
        assert result == "review"

    def test_parse_registered_command_triage(self):
        """Test parsing registered /triage command."""
        comment = "/triage"
        result = parse_command(comment)
        assert result == "triage"

    def test_parse_registered_command_triage_issue(self):
        """Test parsing registered /triage-issue command."""
        comment = "/triage-issue"
        result = parse_command(comment)
        assert result == "triage-issue"

    def test_parse_registered_command_with_args(self):
        """Test parsing registered command with additional arguments."""
        comment = "/review with extra details"
        result = parse_command(comment)
        assert result == "review with extra details"

    def test_parse_unregistered_command_returns_empty(self):
        """Test that unregistered commands return empty string."""
        comment = "/unknown-command"
        result = parse_command(comment)
        assert result == ""

    def test_parse_empty_comment(self):
        """Test parsing empty comment."""
        result = parse_command("")
        assert result == ""

    def test_parse_none_comment(self):
        """Test parsing None comment."""
        result = parse_command(None)
        assert result == ""

    def test_parse_comment_without_command(self):
        """Test parsing comment without any command."""
        comment = "This is just a regular comment"
        result = parse_command(comment)
        assert result == ""

    def test_parse_comment_with_slash_but_no_command(self):
        """Test parsing comment with slash but no valid command."""
        comment = "The path is /home/user"
        result = parse_command(comment)
        assert result == ""

    def test_parse_agent_command_with_leading_whitespace(self):
        """Test parsing /agent command with leading whitespace."""
        comment = "   /agent check this"
        result = parse_command(comment)
        assert result == "check this"

    def test_parse_agent_command_strips_extra_spaces(self):
        """Test that /agent command strips extra spaces."""
        comment = "/agent    multiple   spaces"
        result = parse_command(comment)
        assert result == "multiple   spaces"

    def test_registered_commands_set_contains_expected_commands(self):
        """Test that REGISTERED_COMMANDS contains expected commands."""
        assert "review-pr" in REGISTERED_COMMANDS
        assert "pr-review" in REGISTERED_COMMANDS
        assert "review" in REGISTERED_COMMANDS
        assert "triage" in REGISTERED_COMMANDS
        assert "triage-issue" in REGISTERED_COMMANDS

    def test_parse_command_first_match_wins(self):
        """Test that first matching command is returned."""
        comment = """
        /agent first command
        /review second command
        """
        result = parse_command(comment)
        assert result == "first command"

    def test_parse_slash_only(self):
        """Test parsing comment with just a slash."""
        comment = "/"
        result = parse_command(comment)
        assert result == ""

    def test_parse_agent_without_space(self):
        """Test /agent without space after it."""
        comment = "/agent"
        result = parse_command(comment)
        assert result == ""
