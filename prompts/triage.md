Analyze issue #{issue_number} in {repo} and triage it.

## Steps

1. **Read the issue.** Use `issue_read` with methods `get`, `get_labels`, and `get_comments` (all three in parallel).
2. **Triage.** Based on the issue content, determine:
   - Priority (high, medium, low)
   - Complexity (simple, moderate, complex)
   - Type (bug, feature request, documentation, question, invalid)
3. **Apply labels.** Use `issue_write` with `method: update` to set labels. Do NOT check whether individual labels exist first — just apply them. If a label doesn't exist, GitHub will create it automatically.
4. **Close if clearly invalid.** If the issue is obviously a test, spam, or contains no actionable content (e.g. placeholder text), also set `state: closed` with `state_reason: not_planned` in the same `issue_write` call.
5. **Comment if helpful.** If you closed the issue, or if clarifying questions are needed, use `add_issue_comment` to explain why or to ask questions.
6. **Report.** Post a brief triage assessment as your final message.

## Common labels

bug, enhancement, documentation, question, invalid, wontfix, good first issue, help wanted, duplicate

## Efficiency notes

- Do NOT search all open issues or check individual label existence — these are unnecessary for triaging a single issue.
- Steps 1–3 should take no more than 2–3 turns total.
