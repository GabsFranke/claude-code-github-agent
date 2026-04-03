---
description: "Analyse a session transcript and improve agent instructions via a PR to develop"
argument-hint: "<transcript_summary_path> <workflow_name> <target_repo> [num_turns] [is_error]"
skills:
  - git-worktree-workflow
allowed-tools: ["Bash", "Glob", "Grep", "Read", "Edit", "mcp__github__*"]
---

# Retrospector

Analyse a completed agent session transcript, identify concrete problems with how
the agent performed, and improve the instruction files that govern that agent's
behaviour. Propose changes as a pull request to the `develop` branch.

**Arguments:** "$ARGUMENTS"

- First argument: path to the session transcript summary (markdown, pre-processed)
- Second argument: workflow name (e.g. `review-pr`, `fix-ci`, `triage-issue`, `generic`)
- Third argument: target repository the session worked on (owner/repo, for context)
- Fourth argument (optional): number of turns the session took
- Fifth argument (optional): `error` if the session errored, otherwise omitted

---

## Step 1 — Read the transcript summary

Read the transcript summary at the path given in the first argument.

The file is a structured markdown summary of the session, not the raw JSONL.
It includes:

- Total turn count and error count
- List of subagents invoked (via `@agent-name`)
- Timeline of assistant messages, tool calls, and tool results
- Summary of all tool errors

Scan for:

- Tool calls that returned errors followed by redundant retries
- `@agent-name` references — note which subagents were invoked
- Instructions that were visibly ignored (look for repeated similar tool calls)
- Steps taken in the wrong order (e.g. editing before reading)
- Excessive back-and-forth on something that should have been one step
- Missing context that led to wrong assumptions
- Any explicit failure or error in the final turns

**Note:** The summary is pre-processed to avoid buffer limits. If you need more
detail about a specific turn, the patterns above should be sufficient to identify
instruction gaps.

---

## Step 2 — Decide if improvement is warranted

If the session was clean and effective — instructions followed, few errors, no
redundant loops — write a brief summary to that effect and **stop here**.

Only proceed if you found at least one concrete, actionable problem with the
agent's instructions (not with the user's request or an external service).

---

## Step 3 — Read the instruction files

Determine which instruction files govern the workflow from the second argument.
The mapping is:

| Workflow       | Command file                                      | Agent files                              |
| -------------- | ------------------------------------------------- | ---------------------------------------- |
| `review-pr`    | `plugins/pr-review-toolkit/commands/review-pr.md` | `plugins/pr-review-toolkit/agents/*.md`  |
| `fix-ci`       | `plugins/ci-failure-toolkit/commands/fix-ci.md`   | `plugins/ci-failure-toolkit/agents/*.md` |
| `triage-issue` | `prompts/triage.md`                               | —                                        |
| `generic`      | `prompts/generic.md`                              | —                                        |

**Subagents** — if `@agent-name` appears in the transcript, read the agent file:

- Plugin agents: `plugins/{plugin-name}/agents/{agent-name}.md`
- Python subagents: `subagents/{agent_name}.py` (edit only the `prompt="""..."""` field)

Use `Glob` to discover agent files in a plugin if the exact names are unclear.

**Skills** — skills are invoked via `/skill-name` in the transcript or declared in
a command's frontmatter (`skills:` list). They live at `skills/{skill-name}/SKILL.md`.
If the transcript shows a skill was loaded or invoked, read that file too.
Use `Glob skills/*/SKILL.md` to discover all available skills if needed.

**Also always read this file** — `plugins/retrospector/commands/retrospect.md` —
and evaluate whether your own instructions have gaps that this session exposed.

---

## Step 4 — Formulate precise improvements

For each problem you found, identify:

1. Which instruction file is responsible
2. The exact change needed (add a step, clarify wording, reorder, add an example)
3. Why this change would have prevented the specific failure observed

Be surgical. Do not rewrite files wholesale. Add, amend, or reorder specific
sections only. Preserve all working content.

Do not add generic advice. Every change must be traceable to a specific event
in the transcript.

---

## Step 5 — Create a branch and apply changes

```bash
git checkout -b retrospector/$WORKFLOW-$(date +%Y%m%d-%H%M%S)
```

(Replace `$WORKFLOW` with the workflow name from the second argument.)

Edit each instruction file using the Edit tool with targeted replacements.

For Python subagent files, edit only the string inside `prompt="""..."""` inside
`AgentDefinition(...)`. Do not touch the surrounding Python code.

Stage and commit:

```bash
git add <changed files>
git commit -m "retrospector(<workflow>): <one-line summary of improvements>"
```

Push:

```bash
git push origin HEAD
```

---

## Step 6 — Open a PR to develop

Use `mcp__github__create_pull_request` with `base: develop`.

PR body:

```markdown
## Session that triggered this

- **Workflow:** <workflow>
- **Target repo:** <target_repo>
- **Turns:** <num_turns>
- **Errored:** <yes/no>

## Problems identified

### Problem 1: <short title>

**Observed:** <what happened in the transcript>
**Root cause:** <which instruction was missing or wrong>
**Fix:** <what was changed and why>

(repeat for each problem)

## Files changed

- `<file>` — <one-line reason>

> Files may be commands, agents, prompts, skills (`skills/*/SKILL.md`), or subagents.
```

---

## Rules

- If nothing warrants improvement after Step 2, stop. No commit, no PR.
- Never remove content that is currently working.
- Never change the Python structure around a subagent's `prompt` string.
- Keep instruction files in the same style and format they are already in.
- One PR per session — bundle all file changes into a single commit.
- Your own instructions (`plugins/retrospector/commands/retrospect.md`) are always in scope.
