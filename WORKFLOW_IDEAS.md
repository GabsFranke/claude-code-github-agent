# GitHub Webhook Workflow Ideas

Brainstorming map of every GitHub webhook event to potential agent/plugin workflows.
Not limited by current implementation -- this is a design space exploration.

## PR & Code Review

| Event | Actions | Workflow Idea | Plugin / Agent Type |
|-------|---------|---------------|---------------------|
| `pull_request` | opened, synchronize, reopened, closed, labeled, edited, converted_to_draft, ready_for_review, enqueued, dequeued | Full PR review on open/update. Draft-aware: skip review while draft, re-trigger on ready_for_review. Auto-merge queue monitoring. Label-based routing (e.g. `security` label triggers deep security audit). | `pr-review-toolkit` |
| `pull_request_review` | submitted, dismissed, edited | Monitor review state changes. Alert when a critical review is dismissed. Auto-merge when approved. Escalate stale reviews (>48h without response). | `review-tracker` |
| `pull_request_review_comment` | created, edited, deleted | Sentiment analysis on review comments. Auto-respond to common questions. Track reviewer engagement metrics. | `review-assistant` |
| `pull_request_review_thread` | resolved, unresolved | Auto-resolve threads when the referenced code is changed in a new commit. Alert on long-lived unresolved threads. | `thread-manager` |

## Issues & Project Management

| Event | Actions | Workflow Idea | Plugin / Agent Type |
|-------|---------|---------------|---------------------|
| `issues` | opened, edited, closed, reopened, labeled, unlabeled, assigned, milestoned, demilestoned, pinned, deleted, transferred | Auto-triage on open (labels, priority, area). Route to teams based on area labels. Detect duplicate issues. Generate reproduction steps from bug reports. Close stale issues with notification. | `issue-triage-toolkit` |
| `issue_comment` | created, edited, deleted | Command dispatcher (already have `/review`, `/triage`). Could add: `/investigate`, `/bisect`, `/generate-tests`, `/explain`. Also: auto-respond to common questions, detect toxic language. | `command-router` + `support-bot` |
| `sub_issues` | sub_issue_added, sub_issue_removed, parent_issue_added, parent_issue_removed | Auto-track epic progress. Notify when all sub-issues close. Generate progress reports for epics. | `epic-tracker` |
| `issue_dependencies` | issue_added_as_dependency, issue_removed_as_dependency | Dependency chain validation. Alert when a blocking issue is closed but dependents aren't updated. Critical path analysis. | `dependency-analyzer` |

## CI/CD & Testing

| Event | Actions | Workflow Idea | Plugin / Agent Type |
|-------|---------|---------------|---------------------|
| `workflow_job` | queued, in_progress, completed, waiting | Fix CI failures (already have `fix-ci`). Could add: predict flaky tests from repeated failures, auto-retry known flakes, queue time monitoring and alerting, resource optimization suggestions. | `ci-failure-toolkit` + `flake-detector` |
| `workflow_run` | requested, in_progress, completed | Full pipeline health monitoring. Cross-repo CI dependency tracking. Deployment readiness gates. SLA tracking on CI time. | `pipeline-monitor` |
| `workflow_dispatch` | (none) | On-demand agent tasks triggered from GitHub Actions. E.g.: "run this analysis", "generate changelog", "batch process these files". Custom automation hooks. | `task-runner` |
| `check_run` | created, completed, rerequested, requested_action | Third-party CI integration. Aggregate results from multiple check providers. Auto-rerun known transient failures. Custom check annotations via agent. | `check-aggregator` |
| `check_suite` | completed, requested, rerequested | Suite-level pass/fail gating. Auto-merge when all checks pass. Rollback on regression detection. | `merge-gate` |
| `status` | (none) | Legacy CI status monitoring. Commit status trend analysis. Deployment status tracking per commit. | `status-monitor` |
| `merge_group` | checks_requested, destroyed | Merge queue monitoring. Detect merge train stalls. Auto-resolve merge conflicts in the queue. | `merge-queue-manager` |

## Deployments & Releases

| Event | Actions | Workflow Idea | Plugin / Agent Type |
|-------|---------|---------------|---------------------|
| `deployment` | created | Pre-deploy validation. Generate deployment notes. Smoke test planning. Deployment approval workflows for prod. | `deployment-toolkit` |
| `deployment_status` | created | Post-deploy health checks. Auto-rollback on failure. Monitor error rates after deploy. Deployment pipeline tracking. | `deploy-monitor` |
| `deployment_protection_rule` | requested | Custom deployment gates. Agent evaluates readiness (tests pass, no critical issues, SLO met) and approves/rejects. | `deploy-gate-agent` |
| `deployment_review` | approved, rejected, requested | Deployment approval audit trail. Auto-approve non-prod deployments. Enforce approval policies. | `deploy-policy-enforcer` |
| `release` | created, published, prereleased, released, edited, deleted, unpublished | Auto-generate release notes from PR titles/commits. Publish changelog. Announce on Slack/Discord. Verify release artifacts. Tag-based version validation. | `release-toolkit` |
| `create` | (none) | Branch/tag creation monitoring. Enforce naming conventions. Auto-setup branch protection for release branches. | `branch-manager` |
| `delete` | (none) | Cleanup tracking. Detect accidental branch/tag deletion. Enforce retention policies. | `cleanup-monitor` |

## Code & Repository

| Event | Actions | Workflow Idea | Plugin / Agent Type |
|-------|---------|---------------|---------------------|
| `push` | (none) | Cache warming (already implemented). Could add: auto-index new code, detect large file pushes, enforce commit message conventions, trigger documentation updates. | `push-processor` |
| `commit_comment` | created | Auto-respond to questions about specific commits. Link commits to issues. Detect TODO/FIXME patterns being introduced. | `commit-assistant` |
| `repository_dispatch` | (custom action) | Cross-repo orchestration. Trigger agent workflows from external systems. Webhook-to-agent bridge for non-GitHub tools. Scheduled maintenance tasks. | `orchestrator` |
| `gollum` | (none) | Wiki change monitoring. Auto-format wiki pages. Sync wiki with code docs. Detect stale wiki content. | `wiki-manager` |

## Security & Compliance

| Event | Actions | Workflow Idea | Plugin / Agent Type |
|-------|---------|---------------|---------------------|
| `secret_scanning_alert` | created, reopened, resolved, revoked, publicly_leaked, validated | Auto-rotate leaked secrets. Create incident issues. Notify security team. Verify resolution. | `security-incident-toolkit` |
| `secret_scanning_alert_location` | created | Trace secret exposure. Determine blast radius. Auto-patch affected code. | `secret-tracer` |
| `secret_scanning_scan` | completed | Scan result auditing. Trend tracking. Coverage gap detection. | `scan-auditor` |
| `dependabot_alert` | created, fixed, dismissed, reopened, auto_dismissed, auto_reopened | Auto-fix vulnerable dependencies. Assess severity and prioritize. Generate SBOM updates. | `vuln-fix-toolkit` |
| `code_scanning_alert` | created, fixed, closed_by_user, reopened, appeared_in_branch | Auto-fix code scanning findings. Track remediation SLA. Pattern-based fix suggestions. | `code-scan-fixer` |
| `security_advisory` | published, updated, withdrawn | Monitor GHSA advisories for used dependencies. Auto-assess impact. Pre-emptively patch. | `advisory-monitor` |
| `security_and_analysis` | (none) | Detect when security features are disabled. Audit security settings changes. Enforce security policies. | `security-policy-enforcer` |
| `repository_advisory` | published, reported | Internal vulnerability management. Coordinate responsible disclosure. Auto-create fix branches. | `advisory-manager` |
| `repository_vulnerability_alert` | create, dismiss, resolve, auto_dismissed, auto_reopened, reintroduced | Legacy alert handling. Migrate to dependabot_alert workflows. | `legacy-alert-handler` |

## Repository Management

| Event | Actions | Workflow Idea | Plugin / Agent Type |
|-------|---------|---------------|---------------------|
| `repository` | created, deleted, archived, unarchived, edited, renamed, transferred, privatized, publicized | Repo lifecycle management. Auto-configure new repos (branch protection, CI, labels). Audit setting changes. | `repo-setup-toolkit` |
| `repository_ruleset` | created, edited, deleted | Monitor rule changes. Validate rule consistency across repos. Detect overly permissive rules. | `ruleset-auditor` |
| `branch_protection_configuration` | enabled, disabled | Alert when protection is disabled. Auto-restore protection settings. Compliance auditing. | `protection-monitor` |
| `branch_protection_rule` | created, edited, deleted | Track protection rule changes. Validate rules meet org standards. Detect missing required checks. | `rule-validator` |
| `fork` | (none) | Fork monitoring. Detect suspicious forks of private repos. Welcome fork contributors with setup guide. | `fork-monitor` |
| `deploy_key` | created, deleted | Audit deploy keys. Detect overly permissive keys. Enforce rotation policies. | `key-auditor` |
| `page_build` | (none) | GitHub Pages build failure alerting. Auto-fix common Jekyll/Static site issues. Broken link detection. | `pages-monitor` |

## Projects

| Event | Actions | Workflow Idea | Plugin / Agent Type |
|-------|---------|---------------|---------------------|
| `project` | created, closed, deleted, edited, reopened | Project lifecycle tracking. Auto-archive completed projects. Generate project summaries. | `project-manager` |
| `project_column` | created, deleted, edited, moved | Track WIP limits. Alert on overloaded columns. Auto-sort by priority. | `board-optimizer` |
| `project_card` | created, deleted, edited, moved, converted | Auto-assign cards based on labels. Track card age. Convert cards to issues with templates. | `card-automator` |
| `projects_v2` | created, closed, deleted, edited, reopened | New Projects automation. Auto-populate project fields. Sync with external tools. | `v2-project-sync` |
| `projects_v2_item` | archived, converted, created, deleted, edited, reordered, restored | Item-level automation. Auto-set status based on linked PR state. Field validation. | `v2-item-automator` |
| `projects_v2_status_update` | created, deleted, edited | Status report generation. Trend tracking. Auto-summarize weekly progress. | `status-reporter` |

## Organization & Team

| Event | Actions | Workflow Idea | Plugin / Agent Type |
|-------|---------|---------------|---------------------|
| `organization` | member_added, member_invited, member_removed, renamed, deleted | Onboarding automation. Provision repo access. Send welcome docs. Offboarding cleanup. | `org-onboarding` |
| `team` | created, deleted, edited, added_to_repository, removed_from_repository | Team-repo sync. Auto-add repos to team dashboards. Enforce team naming conventions. | `team-sync` |
| `team_add` | (none) | Track team-repo associations. Audit access patterns. Detect privilege escalation. | `access-auditor` |
| `member` | added, edited, removed | Collaborator change monitoring. Enforce minimum access levels. Audit external collaborator access. | `member-auditor` |
| `membership` | added, removed | Org membership tracking. Sync with HR systems. Auto-revoke on termination. | `membership-sync` |
| `org_block` | blocked, unblocked | Block/unblock audit trail. Auto-investigate blocked user activity. | `block-auditor` |

## App & Installation

| Event | Actions | Workflow Idea | Plugin / Agent Type |
|-------|---------|---------------|---------------------|
| `installation` | created, deleted, new_permissions_accepted, suspend, unsuspend | Installation lifecycle management. Auto-configure repos on install. Clean up on uninstall. Permission change auditing. | `install-manager` |
| `installation_repositories` | added, removed | Track which repos the bot has access to. Auto-index new repos. Clean up caches on removal. | `repo-tracker` |
| `installation_target` | renamed | Handle org/user renames gracefully. Update cached references. | `rename-handler` |
| `github_app_authorization` | revoked | Detect token revocation. Alert on unexpected revocations. | `auth-monitor` |
| `ping` | (none) | Health check. Verify webhook configuration. Log new webhook registrations. | `health-checker` |

## Packages & Marketplace

| Event | Actions | Workflow Idea | Plugin / Agent Type |
|-------|---------|---------------|---------------------|
| `package` | published, updated | Package release automation. Dependency bump PRs. Publish announcements. | `package-release-toolkit` |
| `registry_package` | published, updated | Legacy package handling. Migration to new package event workflows. | `registry-handler` |
| `marketplace_purchase` | purchased, cancelled, changed, pending_change, pending_change_cancelled | License management. Auto-configure purchased tools. Provisioning on purchase. | `license-manager` |
| `personal_access_token_request` | approved, cancelled, created, denied | PAT request auditing. Auto-approve low-risk requests. Enforce token policies. | `pat-policy-enforcer` |

## Custom Properties & Other

| Event | Actions | Workflow Idea | Plugin / Agent Type |
|-------|---------|---------------|---------------------|
| `custom_property` | created, deleted, updated | Track custom property schema changes. Validate property definitions. | `property-auditor` |
| `custom_property_values` | updated | Enforce repo metadata policies. Auto-set properties based on repo content. Compliance validation. | `metadata-enforcer` |
| `meta` | deleted | Webhook lifecycle monitoring. Detect accidental webhook deletion. Critical for operational awareness. | `webhook-monitor` |
| `star` | created, deleted | Community engagement tracking. Thank first-time stargazers. Popularity trend analysis. | `community-tracker` |
| `watch` | started | Watcher engagement metrics. Notify maintainers of growing interest. | `engagement-monitor` |
| `public` | (none) | Visibility change alerting. Security audit on going public. Ensure no secrets in history. Scan for accidentally public internal repos. | `visibility-scanner` |
| `repository_import` | (none) | Import validation. Verify imported repo integrity. Auto-configure imported repos. | `import-validator` |

## High-Priority Combinations

These combine multiple events into cohesive workflows:

### 1. Full PR Lifecycle Agent
```
pull_request.opened → review
pull_request_review.submitted → check approval status
check_suite.completed → verify CI
pull_request.closed → cleanup branches, update issue status
```

### 2. Security Incident Response
```
secret_scanning_alert.created → create incident issue, notify team
secret_scanning_alert_location.created → trace blast radius
dependabot_alert.created → assess and auto-fix
code_scanning_alert.created → generate fix PR
repository_advisory.reported → coordinate disclosure
```

### 3. Deployment Pipeline
```
workflow_dispatch → trigger deployment
deployment.created → pre-deploy checks
deployment_status.created → health checks
release.published → generate notes, announce
```

### 4. Community Management
```
issues.opened → triage and respond
star.created → engagement tracking
fork.created → welcome contributor
pull_request.opened → guide first-time contributors
member.added → onboarding
```

### 5. Compliance Enforcer
```
branch_protection_rule.deleted → alert, auto-restore
repository.publicized → security scan
security_and_analysis → audit changes
deploy_key.created → validate permissions
custom_property_values.updated → policy check
```

### 6. CI Health Monitor
```
workflow_job.completed → track flaky tests, retry known failures
workflow_run.completed → pipeline duration tracking
check_run.completed → aggregate results
status → legacy CI monitoring
merge_group.checks_requested → merge queue health
```
