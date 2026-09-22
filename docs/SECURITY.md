# Threat Model

This document states what this system trusts, what it defends against, and
what it deliberately does not. It is written for someone deciding whether it
is safe to run, and for anyone changing code near a boundary.

The short version: **the host is the trust boundary.** One service is
reachable from the network and it authenticates every request
cryptographically. Everything else is bound to loopback and assumes the host
is not hostile.

---

## 1. What the system does

The bot receives GitHub webhooks, matches them against `workflows.yaml`, and
runs the Claude Agent SDK inside a git worktree with **write access to the
repository**. It can create branches, commit, push, and open pull requests.

That is the security-relevant fact everything else follows from: *causing this
system to run is equivalent to handing someone commit access to the connected
repositories.* Every control below exists to ensure only GitHub can cause
that, and only through events you configured.

## 2. Assets

| Asset | Why it matters |
|---|---|
| GitHub App private key (`GITHUB_PRIVATE_KEY`) | Mints installation tokens for every connected repo. Full compromise. |
| Installation tokens | Short-lived repository write access. |
| `GITHUB_WEBHOOK_SECRET` | The only thing distinguishing GitHub from any other caller. |
| Anthropic API credentials | Billable; can be abused for compute. |
| `REDIS_PASSWORD` | Guards the job queue: writing to it means running arbitrary workflows. |
| Repository contents and history | The agent reads and writes them. |
| Session transcripts (`/var/transcripts`, Redis) | May contain source code and prompt content. |

## 3. Trust boundary and exposure

Exposure is enforced in `docker-compose.yml`, not by convention:

| Service | Binding | Authenticated | Notes |
|---|---|---|---|
| `webhook` | `0.0.0.0:10000` | **Yes**: HMAC-SHA256 | The only intentional ingress. GitHub must reach it. |
| `session-service` | `127.0.0.1:10001` | No | Loopback only. See §6.1. |
| `mcp_proxy` | `127.0.0.1:18000` | No | Loopback only. |
| `redis` | `127.0.0.1:6379` | Password | Loopback only. |
| workers | not published | n/a | Reach Redis over the compose network. |

Anything on this host, and any process in the compose network, is trusted.
Anything off the host reaches exactly one port, and that port verifies
signatures.

## 4. Threats addressed

Each of these is enforced in code and covered by a test that fails if the
control is removed.

### T1: Forged webhook requests

**Threat.** Anyone who can reach port 10000 posts a crafted `issue_comment`
payload containing `/agent`, and the bot executes against your repository.

**Control.** `services/webhook/validators/signature_validator.py` verifies
`X-Hub-Signature-256` with `hmac.compare_digest` (constant-time). Failure
returns 401 before the payload is parsed or queued.

**Fail-closed.** If `GITHUB_WEBHOOK_SECRET` is empty the service **refuses to
start**, unless `ALLOW_UNSIGNED_WEBHOOKS=true` is set explicitly, which logs a
warning on every boot. A missing secret can never silently mean "trust
everyone".

**Tested.** `tests/webhook/test_delivery_dedup.py::TestSignatureIsMandatory`,
`tests/integration/test_webhook_handlers.py::TestWebhookValidation`.

### T2: Replayed deliveries

**Threat.** GitHub retries a delivery, or someone replays a captured signed
request, and the agent runs twice, producing duplicate PRs, duplicate comments and
duplicate spend. A valid signature stays valid forever, so signing alone does
not stop this.

**Control.** `shared/webhook_dedup.py` claims each `X-GitHub-Delivery` id with
Redis `SET NX EX` (24 h). The claim is taken *after* signature verification,
so an unauthenticated caller cannot burn a guessed id to suppress the genuine
delivery. It is released when handling fails unexpectedly, so GitHub's retry
still gets through.

**Tested.** `tests/shared/test_webhook_dedup.py`,
`tests/webhook/test_delivery_dedup.py`.

### T3: Command injection through comment bodies

**Threat.** A comment body is parsed for a `/command` and used to select a
workflow. A crafted command escapes into a shell or selects an unintended
workflow.

**Control.** Commands are matched against `^/[a-z0-9\-]+$` and capped at 50
characters (`services/webhook/main.py`). They are used only as dictionary keys
into workflows loaded from `workflows.yaml`, never interpolated into a shell.
Git operations use list-form `execute_git_command`; the string form is
deprecated and warns.

**Tested.** `tests/webhook/test_payload_extractor.py`,
`tests/unit/test_git_utils.py`.

### T4: Infinite self-trigger loops

**Threat.** The bot opens a PR, its own event fires the workflow again, and it
loops, burning API spend without bound.

**Control.** `skip_self` compares the event `sender.login` against
`WEBHOOK_BOT_USERNAME` and drops the event.

**Tested.** `tests/workflows/test_skip_self.py`,
`tests/workflows/test_skip_self_integration.py`.

### T5: Credential leakage into the repository

**Threat.** Secrets committed to git, or written into a worktree the agent
then pushes.

**Control.** `.env` is gitignored and never committed; all configuration is
injected via `env_file`/`environment`. Git credentials are written to a
**per-job** `.git-credentials` inside the disposable worktree
(`services/sandbox_executor/git_setup.py`) and die with it. The shared bare
cache under `/var/cache/repos` never holds a token: `repo_sync` authenticates
each clone and fetch with a per-command `http.extraheader` and keeps the
stored remote URL token-free (`services/repo_sync/sync_worker.py`,
`git_auth_args`), so nothing outlives the command that used it.
`.gitguardian.yaml` and `.pre-commit-config.yaml` scan commits. Test
credentials come from `TEST_REDIS_PASSWORD`/`REDIS_PASSWORD`, never literals,
and the RSA key used in tests is generated per session rather than stored.

**Tested.** `tests/repo_sync/test_sync_worker.py::TestGitAuthArgs` and the
clone/fetch cases in `TestProcessSyncRequest`, which assert the token appears
only as a header and never in a URL.

### T6: Unbounded resource consumption

**Threat.** A malicious or runaway job exhausts the host.

**Control.** `sandbox_worker` runs under `mem_limit: 4g`. Each SDK run is
capped by `sdk_timeout` (default 1800 s), and `MAX_AUTO_CONTINUES`
(default 10) bounds auto-continue chains.
`shared/rate_limiter.py` bounds GitHub and Anthropic call rates,
`shared/retry.py` bounds retries with backoff, and `shared/dlq.py` captures
poison messages instead of looping on them.

## 5. Explicitly out of scope

These are accepted risks for a single-tenant, self-hosted deployment. They are
decisions, not oversights, but they are the first things to revisit if the
deployment model changes.

- **A hostile host.** Root on the host, or any process that can read `.env` or
  reach the Docker socket, owns the system. No defence is attempted.
- **A malicious operator.** Whoever configures `workflows.yaml` decides what
  the agent does. There is no separation between operator and administrator.
- **Repository collaborators.** Any GitHub user who can comment on a connected
  repository can invoke the agent: there is no per-actor allowlist. On a
  private repo with trusted collaborators this is the intended behaviour. **On
  a public repository it is a privilege escalation**: any stranger who can open
  an issue can run the agent. Do not connect this to a public repository
  without adding actor filtering first.
- **The agent's own judgement.** The SDK runs with
  `permission_mode="acceptEdits"` and auto-approved GitHub tools. The agent is
  trusted to act reasonably within its worktree; prompt injection via
  repository content is not defended against. Treat every connected repository
  as content the agent will read and may act on.
- **Multi-tenancy.** One `.env`, one GitHub App, one Redis. Sessions are
  namespaced by repo and thread but not isolated by tenant.
- **Encryption at rest.** Transcripts and Redis data are unencrypted on disk.

## 6. Known weaknesses

### 6.1 `session-service` has no authentication

Its routes are keyed by a `token` that is base64 of
`(repo, thread_type, thread_id, workflow)`: an identifier, not a secret.
Anyone who can reach the port can enumerate tokens, read transcripts, and
inject messages into a running session.

**Mitigation:** the service is bound to `127.0.0.1` in `docker-compose.yml`,
so reaching it requires host access, which is already outside the trust
boundary (§5). Reach it from another machine with an SSH tunnel rather than
widening the port binding. If it ever needs to be exposed, real per-session
authorization must be added first.

### 6.2 Installation tokens are not refreshed mid-run

A job mints one installation token when its worktree is set up and uses it
for the whole run, including automatic continuations. `GitHubAuthService`
refreshes tokens before expiry on every call, but the sandbox calls it once
per job. GitHub installation tokens live for 60 minutes; the default
`sdk_timeout` is 30 minutes, so a single run cannot outlive its token at the
defaults, but a long `sdk_timeout` or a chain of continuations can. A job
whose token expires fails its next GitHub call rather than being refreshed.

**Mitigation:** keep `sdk_timeout` below 60 minutes. Refreshing mid-run would
require rewriting the worktree credential file, `.mcp.json`, and the agent's
environment while the SDK is executing, and has not been done.

### 6.3 No rollback drill

`docker-compose.yml` is reproducible from a clean clone, but no rollback
procedure has been exercised and no monitoring has been shown to detect an
induced failure.

## 7. Verifying the controls

```bash
# T1: signature verification, including fail-closed on a missing secret
pytest tests/webhook/test_delivery_dedup.py::TestSignatureIsMandatory -v

# T2: replay protection, claim and release behaviour
pytest tests/shared/test_webhook_dedup.py tests/webhook/test_delivery_dedup.py -v

# T3/T4: command validation and self-trigger prevention
pytest tests/webhook/ tests/workflows/test_skip_self.py -v

# Confirm exposure matches section 3: only port 10000 lacks a 127.0.0.1 host_ip
docker compose config | grep -A2 published

# Confirm the startup guard refuses an unset secret
GITHUB_WEBHOOK_SECRET= docker compose up webhook
```

## 8. Reporting

This is a personal, self-hosted project with no security support commitment.
Open an issue for anything found.
