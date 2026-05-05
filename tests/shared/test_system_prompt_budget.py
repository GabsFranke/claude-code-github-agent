"""Tests for system prompt budget enforcement in SDKOptionsBuilder."""

from shared.sdk_factory import SYSTEM_PROMPT_BUDGET, SDKOptionsBuilder, _truncate_text


class TestTruncateText:
    def test_short_text_fits(self):
        result = _truncate_text("hello world", 100)
        assert result == "hello world"

    def test_truncates_long_text(self):
        lines = [f"line {i} with some content" for i in range(100)]
        text = "\n".join(lines)
        result = _truncate_text(text, 20)
        assert result is not None
        assert "truncated" in result
        assert len(result) < len(text)

    def test_returns_none_for_impossible_budget(self):
        result = _truncate_text("a" * 1000, 0)
        assert result is None


class TestStructuralContext:
    def test_with_structural_context(self):
        builder = SDKOptionsBuilder(cwd="/tmp")
        builder.with_structural_context(
            file_tree="root/\n  src/\n    main.py",
        )
        assert builder._structural_context is not None
        assert "<repo_structure>" in builder._structural_context

    def test_with_empty_structural_context(self):
        builder = SDKOptionsBuilder(cwd="/tmp")
        builder.with_structural_context(file_tree="")
        assert builder._structural_context is None

    def test_with_file_tree_only(self):
        builder = SDKOptionsBuilder(cwd="/tmp")
        builder.with_structural_context(file_tree="root/\n  main.py")
        assert builder._structural_context is not None
        assert "<repo_structure>" in builder._structural_context


class TestSystemPromptBudget:
    def test_budget_constant(self):
        assert SYSTEM_PROMPT_BUDGET == 12_000

    def test_all_components_fit(self):
        """When total is under budget, nothing is truncated."""
        builder = SDKOptionsBuilder(cwd="/tmp")
        builder.with_system_prompt("Short workflow context")
        builder.with_repository_context(
            claude_md="Short claude md",
            memory_index="Short memory",
        )
        builder.with_structural_context(
            file_tree="root/\n  main.py",
        )

        result = builder._assemble_system_prompt()
        assert result is not None
        # All components should be present
        assert "Short workflow context" in result
        assert "Short claude md" in result
        assert "Short memory" in result
        assert "<repo_structure>" in result

    def test_returns_none_when_empty(self):
        builder = SDKOptionsBuilder(cwd="/tmp")
        result = builder._assemble_system_prompt()
        assert result is None

    def test_structural_context_in_build(self):
        """Build should include structural context in the system prompt."""
        builder = SDKOptionsBuilder(cwd="/tmp")
        builder.with_system_prompt("Test prompt")
        builder.with_structural_context(
            file_tree="root/\n  main.py",
        )

        # Need to mock ClaudeAgentOptions since it's mocked in conftest
        builder.build()
        # The build method sets _system_prompt via _assemble_system_prompt
        assert builder._system_prompt is not None
        assert "<repo_structure>" in builder._system_prompt
