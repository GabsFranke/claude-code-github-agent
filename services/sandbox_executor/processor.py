import asyncio
import logging
import os
import shutil
import subprocess
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

from repo_setup import RepoSetupEngine
from shared import (
    JobQueue,
    RepositorySyncError,
    SDKError,
    SDKTimeoutError,
    WorktreeCreationError,
    execute_git_command,
    wait_for_repo_sync,
)
from shared.constants import (
    CLOSED_SESSION_TTL_HOURS,
    FALLBACK_CONVERSATION_TTL_HOURS,
    MAX_AUTO_CONTINUES as MAX_AUTO_CONTINUES_CONST,
    session_dedup_key,
)
from shared.context_builder import generate_structural_context
from shared.sdk_executor import execute_sdk
from shared.sdk_factory import SDKOptionsBuilder
from shared.session_store import SessionStore
from shared.utils import build_session_url
from shared.worktree_lock import WorktreeKey, WorktreeLock
from shared.worktree_manager import get_worktree_path, reuse_or_create_worktree

from .git_setup import configure_git, credentials_path, init_submodules
from .utils import configure_builder, find_transcript_path, write_transcript_meta

logger = logging.getLogger(__name__)

SDK_MAX_RETRIES = int(os.getenv("SDK_MAX_RETRIES", "3"))
SDK_RETRY_BASE_DELAY = float(os.getenv("SDK_RETRY_BASE_DELAY", "5.0"))


class JobProcessor:
    def __init__(self, job_queue: JobQueue, job_id: str, job_data: dict):
        self.job_queue = job_queue
        self.job_id = job_id
        self.job_data = job_data

        self.workspace: str = ""
        self.repo_dir: str = ""
        self.builder: Any = None
        self.persist_session: bool = False
        self.session_mode: str = str(job_data.get("session_mode") or "new")
        self.session_id: str | None = job_data.get("session_id")
        self.worktree_lock: WorktreeLock | None = None
        self.worktree_key: WorktreeKey | None = None
        self.streaming_bridge: Any = None
        self.streaming_control: Any = None
        self.user_interrupt_event: asyncio.Event = asyncio.Event()
        self.session_service = SessionStore(self.job_queue.redis)

        self.repo = job_data.get("repo", "unknown")
        self.issue_number = job_data.get("issue_number", 0)
        self.ref = job_data.get("ref", "main")
        self.thread_type = job_data.get("thread_type", "issue")
        self.thread_id = str(self.issue_number)
        self.workflow_name = str(job_data.get("workflow_name") or "generic")

        # Context details
        self.file_tree_text = ""
        self.thread_history_text = ""
        self.parent_span_id = job_data.get("parent_span_id")

    async def process(self):
        try:
            if not self._validate_job():
                return

            await self._setup_worktree()
            await configure_git(
                self.workspace, self.job_data["github_token"], self.job_id
            )
            await init_submodules(self.workspace, self.repo)
            if self.session_mode not in ("resume", "continue"):
                await self._run_repo_setup()
            else:
                logger.info(
                    f"Skipping repo setup on {self.session_mode} — "
                    f"worktree already provisioned"
                )
            await self._prepare_context()
            result = await self._execute_sdk_loop()

            if result:
                await self._save_session(result)
                if result.get("is_cancelled"):
                    await self._mark_cancelled(result)
                elif result.get("is_error"):
                    await self._handle_error(
                        SDKError(
                            f"SDK reported error: {result.get('response', 'unknown error')[:200]}"
                        )
                    )
                else:
                    await self._mark_success(result)

        except Exception as e:
            await self._handle_error(e)
        finally:
            await self._cleanup()

    def _validate_job(self) -> bool:
        try:
            uuid.UUID(self.job_id)
            return True
        except (ValueError, AttributeError):
            logger.error(f"Invalid job_id format: {self.job_id}")
            asyncio.create_task(
                self.job_queue.complete_job(
                    self.job_id,
                    {
                        "status": "error",
                        "error": f"Invalid job_id format: {self.job_id}",
                        "repo": self.repo,
                        "issue_number": self.issue_number,
                    },
                    status="error",
                )
            )
            return False

    async def _setup_worktree(self):
        logger.info(f"Job data keys: {list(self.job_data.keys())}")
        logger.info(f"Job data ref value: {self.job_data.get('ref', 'NOT_FOUND')}")
        logger.info(f"Setting up worktree for {self.repo} (ref {self.ref})")

        # Always regenerate the token when installation_id is available.
        # Reclaimed jobs may carry a stale (expired) token from backup, and
        # checking "if not github_token" would skip regeneration for those.
        #
        # Fall back through: job_data.installation_id → event_data →
        # GITHUB_INSTALLATION_ID env var (last resort for sessions where
        # the transcript meta was lost before the fix was applied).
        installation_id = (
            self.job_data.get("installation_id")
            or self.job_data.get("event_data", {}).get("installation_id", "")
            or os.getenv("GITHUB_INSTALLATION_ID", "")
        )
        if installation_id:
            try:
                from shared.github_auth import GitHubAuthService

                auth = GitHubAuthService(installation_id=str(installation_id))
                async with auth:
                    self.job_data["github_token"] = await auth.get_token()
                logger.info(
                    f"Generated GitHub token from installation_id {installation_id}"
                )
            except Exception as e:
                logger.warning(
                    f"Failed to generate GitHub token from installation_id: {e}"
                )

        # Ensure we have a valid GitHub token before proceeding.
        # Resume/reclaim jobs carry installation_id but may not include
        # github_token — if regeneration fails, fail the job explicitly
        # instead of crashing later with a cryptic KeyError in _configure_git
        # or _execute_sdk_loop.
        if not self.job_data.get("github_token"):
            raise WorktreeCreationError(
                "GitHub token unavailable: installation_id missing or "
                "token generation failed. Ensure GITHUB_APP_ID, "
                "GITHUB_PRIVATE_KEY, and installation_id are configured."
            )

        self.repo_dir = await wait_for_repo_sync(
            self.repo, self.ref, self.job_queue.redis
        )

        conversation_config = self.job_data.get("conversation_config") or {}
        self.persist_session = conversation_config.get("persist", False)
        ttl_hours = conversation_config.get(
            "ttl_hours", FALLBACK_CONVERSATION_TTL_HOURS
        )
        logger.info(
            f"Session config: persist={self.persist_session}, ttl={ttl_hours}h, "
            f"mode={self.session_mode}, workflow={self.workflow_name}, "
            f"conversation_config={conversation_config}"
        )

        if self.persist_session:
            self.worktree_key = WorktreeKey(
                repo=self.repo,
                thread_type=self.thread_type,
                thread_id=self.thread_id,
                workflow=self.workflow_name,
            )
            self.worktree_lock = WorktreeLock(self.job_queue.redis, self.worktree_key)

            acquired = await self.worktree_lock.acquire(self.job_id, timeout=0)
            if not acquired:
                lock_info = await self.worktree_lock.get_lock_info()
                logger.info(
                    f"Worktree locked by job {lock_info.job_id if lock_info else 'unknown'}, "
                    f"setting pending prompt and waiting..."
                )
                await self.worktree_lock.set_pending_prompt(
                    self.job_id, self.job_data.get("prompt", "")
                )
                await self.worktree_lock.send_cancel_signal()

                released = await self.worktree_lock.wait_for_release(timeout=300)
                if not released:
                    raise WorktreeCreationError("Timeout waiting for worktree lock")

                acquired = await self.worktree_lock.acquire(self.job_id, timeout=0)
                if not acquired:
                    raise WorktreeCreationError("Failed to acquire worktree lock")

                pending_prompt = await self.worktree_lock.get_pending_prompt()
                if pending_prompt:
                    logger.info(f"Resuming with pending prompt for {self.worktree_key}")
                    self.job_data["prompt"] = pending_prompt.prompt

                    session_store = SessionStore(self.job_queue.redis)
                    existing_session = await session_store.get_session(
                        self.repo, self.thread_type, self.thread_id, self.workflow_name
                    )
                    if existing_session and existing_session.session_id:
                        self.session_mode = "resume"
                        self.session_id = existing_session.session_id
                        logger.info(
                            f"Resuming interrupted session {self.session_id[:8]}... with new prompt"
                        )
                    else:
                        self.session_mode = "new"
                        logger.warning(
                            "Pending prompt found but no previous session, starting fresh"
                        )

            worktree_path = get_worktree_path(
                self.repo, self.thread_type, self.thread_id, self.workflow_name
            )
            await reuse_or_create_worktree(
                bare_repo=self.repo_dir,
                ref=self.ref,
                worktree_path=worktree_path,
                session_mode=self.session_mode,
            )
            self.workspace = str(worktree_path)
            logger.info(
                f"Using deterministic worktree: {self.workspace} (mode={self.session_mode})"
            )
        else:
            ephemeral_base = Path.home() / ".claude" / "worktrees" / "ephemeral"
            ephemeral_base.mkdir(parents=True, exist_ok=True)
            workspace_base = tempfile.mkdtemp(
                prefix=f"job_{self.job_id[:8]}_",
                dir=str(ephemeral_base),
            )
            os.rmdir(workspace_base)
            self.workspace = workspace_base
            logger.info(
                f"Created ephemeral workspace for job {self.job_id}: {self.workspace}"
            )

            if self.ref.startswith("refs/pull/") or self.ref.startswith("refs/tags/"):
                bare_ref = self.ref
            else:
                base_ref = (
                    self.ref.replace("refs/heads/", "")
                    if self.ref.startswith("refs/heads/")
                    else self.ref.replace("refs/", "")
                )
                bare_ref = f"refs/remotes/origin/{base_ref}"

            wt_cmd = [
                "git",
                f"--git-dir={self.repo_dir}",
                "worktree",
                "add",
                "--detach",
                self.workspace,
                bare_ref,
            ]
            code, _out, err = await execute_git_command(wt_cmd)

            if code != 0:
                logger.warning(
                    f"Worktree ref {bare_ref} failed: {err}. Trying to detect default branch..."
                )
                list_cmd = [
                    "git",
                    f"--git-dir={self.repo_dir}",
                    "branch",
                    "--list",
                    "-r",
                ]
                list_code, list_out, list_err = await execute_git_command(list_cmd)
                default_branch = "refs/remotes/origin/main"
                if list_code == 0 and list_out:
                    branches = [
                        b.strip()
                        for b in list_out.split("\n")
                        if b.strip() and "origin/" in b
                    ]
                    if branches:
                        branch_name_only = branches[0].replace("origin/", "")
                        default_branch = f"refs/remotes/origin/{branch_name_only}"
                        logger.info(f"Detected default branch: {default_branch}")
                else:
                    logger.warning(
                        f"Could not list branches: {list_err}. Using fallback: {default_branch}"
                    )

                wt_cmd_fb = [
                    "git",
                    f"--git-dir={self.repo_dir}",
                    "worktree",
                    "add",
                    "--detach",
                    self.workspace,
                    default_branch,
                ]
                code, _out, err = await execute_git_command(wt_cmd_fb)
                if code != 0:
                    raise WorktreeCreationError(f"Failed to create worktree: {err}")

    async def _run_repo_setup(self):
        try:
            setup_engine = RepoSetupEngine()
            setup_config = setup_engine.get_setup_config(self.repo)

            if setup_config:
                logger.info(f"Found setup configuration for {self.repo}")
                setup_result = await setup_engine.run_setup(
                    self.workspace, self.repo, setup_config
                )

                if not setup_result["all_successful"]:
                    logger.warning(
                        f"Some setup commands failed for {self.repo}, continuing anyway..."
                    )
                    self._log_setup_failures(setup_result["results"])
                else:
                    logger.info(
                        f"Setup completed successfully for {self.repo} "
                        f"in {setup_result['elapsed_seconds']:.1f}s"
                    )
        except Exception as e:
            logger.warning(
                f"Error during repository setup for {self.repo}: {e}. "
                f"Continuing with job execution...",
                exc_info=True,
            )

    @staticmethod
    def _log_setup_failures(results: list) -> None:
        """Log individual failed setup commands from a setup result."""
        for result in results:
            if not result.get("success"):
                cmd_info = result.get("commands", result.get("command", "unknown"))
                if isinstance(cmd_info, list):
                    cmd_info = " && ".join(cmd_info)
                logger.warning(
                    f"Failed command(s): {cmd_info} - "
                    f"{result.get('error', 'unknown error')}"
                )

    async def _prepare_context(self):
        try:
            self.file_tree_text = await generate_structural_context(
                repo_path=Path(self.workspace),
            )
            logger.info(
                f"Generated structural context: file_tree={len(self.file_tree_text)} chars"
            )
        except Exception as e:
            logger.warning(
                f"Structural context generation failed, continuing without: {e}",
                exc_info=True,
            )

        try:
            from shared.thread_history import (
                ThreadHistoryConfig,
                fetch_and_format_thread_history,
            )

            context_profile_data = self.job_data.get("context_profile", {})
            th_raw = context_profile_data.get("thread_history", {})
            thread_config = ThreadHistoryConfig(**th_raw)
            if thread_config.enabled and self.issue_number:
                self.thread_history_text = await fetch_and_format_thread_history(
                    repo=self.repo,
                    issue_number=self.issue_number,
                    token=self.job_data["github_token"],
                    thread_type=self.thread_type,
                    config=thread_config,
                )
                if self.thread_history_text:
                    logger.info(
                        f"Fetched thread history: {len(self.thread_history_text)} chars"
                    )
                else:
                    logger.info("No thread history available")
        except Exception as e:
            logger.warning(
                f"Thread history fetch failed, continuing without: {e}", exc_info=True
            )

        # Write .mcp.json
        from shared.mcp_json_writer import write_mcp_json

        write_mcp_json(worktree_path=self.workspace, repo=self.repo)

        # Initialize CodeGraph index (installed in Dockerfile; graceful fallback
        # if run outside Docker or binary is missing)
        try:
            subprocess.run(
                ["codegraph", "init", "-i"],
                cwd=self.workspace,
                timeout=60,
                capture_output=True,
                check=False,
            )
            logger.info("CodeGraph index initialized for %s", self.workspace)
        except FileNotFoundError:
            logger.info("CodeGraph not installed, skipping index build")
        except subprocess.TimeoutExpired:
            logger.warning("CodeGraph init timed out for %s", self.workspace)
        except Exception as e:
            logger.debug("CodeGraph init skipped: %s", e)

    async def _execute_sdk_loop(self):
        github_token = self.job_data["github_token"]
        system_context = self.job_data.get("system_context")
        claude_md = self.job_data.get("claude_md")
        memory_index = self.job_data.get("memory_index")

        # Model tier from workflows.yaml (opus/sonnet/haiku). Unset means the
        # CLI resolves the model itself; see tests/shared/test_model_resolution.py.
        model_tier = self.job_data.get("model")

        os.environ["GITHUB_TOKEN"] = github_token
        if github_token:
            logger.info(f"GitHub token available: {len(github_token)} characters")
        else:
            logger.warning("No GitHub token provided to sandbox executor")

        # Session streaming setup
        if self.job_data.get("streaming_enabled") and self.job_data.get(
            "session_token"
        ):
            session_token = self.job_data["session_token"]
            try:
                from shared.session_stream import ControlChannel, SessionStreamBridge

                self.streaming_bridge = SessionStreamBridge(
                    token=session_token, redis=self.job_queue.redis
                )
                self.streaming_control = ControlChannel(
                    token=session_token,
                    redis=self.job_queue.redis,
                    interrupt_event=self.user_interrupt_event,
                )
                await self.streaming_control.start()

                await self.streaming_bridge.publish_init(
                    repo=self.repo,
                    issue_number=self.issue_number,
                    workflow=self.workflow_name,
                )
                session_meta = await self.session_service.get_streaming_session(
                    session_token
                )
                run_count = session_meta.run_count if session_meta else 1
                await self.streaming_bridge.publish(
                    "run_start",
                    {"run_number": run_count, "session_id": self.session_id},
                )

                logger.info(
                    f"[Streaming] Session streaming enabled for token {session_token[:8]}..."
                )
            except Exception as e:
                logger.warning(
                    f"[Streaming] Failed to set up streaming for {session_token[:8]}...: {e}. Continuing without streaming."
                )
                self.streaming_bridge = None
                self.streaming_control = None

        result: dict | None = None
        sdk_task: asyncio.Task | None = None
        current_prompt = self.job_data["prompt"]
        current_session_id = self.session_id
        interrupted = False

        async def handle_cancel():
            nonlocal interrupted
            interrupted = True
            logger.info(
                f"Cancel signal received, interrupting SDK for {self.worktree_key}"
            )
            if sdk_task and not sdk_task.done():
                sdk_task.cancel()

        for continue_count in range(MAX_AUTO_CONTINUES_CONST + 1):
            interrupted = False
            self.user_interrupt_event.clear()

            builder = SDKOptionsBuilder(cwd=self.workspace)
            if model_tier:
                builder = builder.with_model(model_tier)
            builder = configure_builder(
                builder,
                repo=self.repo,
                workflow_name=self.workflow_name,
                ref=self.ref,
                parent_span_id=self.parent_span_id,
                system_context=system_context,
                claude_md=claude_md,
                memory_index=memory_index,
                thread_history_text=self.thread_history_text,
                file_tree_text=self.file_tree_text,
            )

            if continue_count == 0:
                if self.session_mode == "resume" and current_session_id:
                    logger.info(f"Resuming session {current_session_id[:8]}...")
                    builder = builder.with_session_resume(current_session_id)
                elif self.session_mode == "fork" and current_session_id:
                    logger.info(f"Forking from session {current_session_id[:8]}...")
                    builder = builder.with_session_fork(current_session_id)
                elif self.session_mode == "continue":
                    logger.info("Continuing most recent session...")
                    builder = builder.with_session_continue()
            else:
                if current_session_id:
                    builder = builder.with_session_resume(current_session_id)

            conversation_summary = self.job_data.get("conversation_summary")
            if (
                conversation_summary
                and self.session_mode in ("resume", "continue")
                and continue_count == 0
            ):
                summary_context = (
                    f"\n\n## Previous Conversation Context\n{conversation_summary}"
                )
                builder = builder.with_system_prompt(
                    (system_context or "") + summary_context
                )

            if self.streaming_bridge:
                builder.with_streaming(self.streaming_bridge)
                session_proxy_url = (
                    os.getenv("SESSION_PROXY_URL", "").strip().rstrip("/")
                )
                if session_proxy_url:
                    owner, _, repo_name = self.repo.partition("/")
                    session_url = build_session_url(
                        session_proxy_url,
                        owner,
                        repo_name,
                        self.thread_type,
                        self.issue_number,
                        self.workflow_name,
                    )
                    builder.with_session_signature(session_url)

            self.builder = builder

            if self.worktree_lock:
                async with self.worktree_lock.cancel_subscription(handle_cancel):
                    sdk_task = asyncio.create_task(
                        execute_sdk(
                            prompt=current_prompt,
                            options=builder.build(),
                            max_retries=SDK_MAX_RETRIES,
                            retry_base_delay=SDK_RETRY_BASE_DELAY,
                            streaming_bridge=self.streaming_bridge,
                        )
                    )

                    done = False
                    while not done:
                        sdk_done_task = asyncio.ensure_future(sdk_task)
                        interrupt_wait = asyncio.create_task(
                            self.user_interrupt_event.wait()
                        )
                        try:
                            await asyncio.wait(
                                [sdk_done_task, interrupt_wait],
                                return_when=asyncio.FIRST_COMPLETED,
                            )
                        except asyncio.CancelledError:
                            interrupted = True
                            if not sdk_task.done():
                                sdk_task.cancel()
                            break

                        if sdk_done_task.done():
                            interrupt_wait.cancel()
                            try:
                                result = sdk_done_task.result()
                            except asyncio.CancelledError:
                                interrupted = True
                                await self.worktree_lock.set_interrupted()
                            done = True
                        else:
                            sdk_done_task.cancel()
                            interrupt_wait.cancel()
                            if not sdk_task.done():
                                sdk_task.cancel()
                                try:
                                    await sdk_task
                                except asyncio.CancelledError:
                                    pass
                            interrupted = True
                            done = True
            else:
                result = await execute_sdk(
                    prompt=current_prompt,
                    options=builder.build(),
                    max_retries=SDK_MAX_RETRIES,
                    retry_base_delay=SDK_RETRY_BASE_DELAY,
                    streaming_bridge=self.streaming_bridge,
                )

            if interrupted and not self.user_interrupt_event.is_set():
                logger.info(f"Job {self.job_id} interrupted, marking as superseded")
                await self.job_queue.complete_job(
                    self.job_id,
                    {
                        "status": "superseded",
                        "repo": self.repo,
                        "issue_number": self.issue_number,
                        "message": "Interrupted by new prompt, session saved for continuation",
                    },
                    status="superseded",
                )
                logger.info(f"Job {self.job_id} completed as superseded")
                return None

            if not result and not interrupted:
                raise RuntimeError("SDK execution returned no result")

            if self.streaming_bridge is not None and self.job_data.get("session_token"):
                inbox_messages = await self.session_service.pop_inbox_messages(
                    self.job_data["session_token"]
                )

                if inbox_messages:
                    current_prompt = inbox_messages[0]
                    if len(inbox_messages) > 1:
                        current_prompt += (
                            "\n\n(Follow-up: " + "; ".join(inbox_messages[1:]) + ")"
                        )
                    current_session_id = result.get("session_id") if result else None

                    await self.streaming_bridge.publish_user_message(inbox_messages[0])
                    await self.streaming_bridge.publish_init(
                        repo=self.repo,
                        issue_number=self.issue_number,
                        workflow=self.workflow_name,
                    )
                    await self.session_service.set_running(
                        self.job_data["session_token"]
                    )
                    logger.info(
                        f"[Streaming] Auto-continue #{continue_count + 1} for {self.job_data['session_token'][:8]}..."
                    )
                    continue

            if interrupted and self.user_interrupt_event.is_set() and not result:
                logger.info(f"Job {self.job_id} stopped by user via stop_agent command")
                result = {
                    "is_error": False,
                    "session_id": current_session_id,
                    "num_turns": 0,
                    "response": "Stopped by user",
                    "is_cancelled": True,
                }

            break

        if self.streaming_bridge is not None:
            try:
                is_error = (result or {}).get("is_error", False)
                new_session_id = (result or {}).get("session_id") if result else None
                if self.job_data.get("session_token"):
                    await self.session_service.set_completed(
                        token=self.job_data["session_token"],
                        is_error=is_error,
                        repo=self.repo,
                        issue_number=self.issue_number,
                        workflow=self.workflow_name,
                        session_id=new_session_id,
                    )
                await self.streaming_bridge.close()
            except Exception as e:
                logger.warning(f"[Streaming] Error during streaming teardown: {e}")
        if self.streaming_control is not None:
            try:
                await self.streaming_control.stop()
            except Exception as e:
                logger.warning(f"[Streaming] Error stopping control channel: {e}")

        if self.builder is not None:
            await self.builder.flush_pending_post_jobs()
        return result

    async def _save_session(self, result):
        new_session_id = result.get("session_id")
        logger.info(
            f"Session result: id={new_session_id}, persist={self.persist_session}, cwd={self.workspace}"
        )

        if new_session_id and self.persist_session:
            try:
                issue_state = self.job_data.get("event_data", {}).get(
                    "issue_state", "open"
                )
                ttl_hours = (
                    CLOSED_SESSION_TTL_HOURS
                    if issue_state == "closed"
                    else self.job_data.get("conversation_config", {}).get(
                        "ttl_hours", FALLBACK_CONVERSATION_TTL_HOURS
                    )
                )
                if issue_state == "closed":
                    logger.info(
                        f"Issue is closed, overriding session TTL to {ttl_hours}h"
                    )

                session_store = SessionStore(self.job_queue.redis)
                await session_store.save_session(
                    repo=self.repo,
                    thread_type=self.thread_type,
                    thread_id=self.thread_id,
                    workflow=self.workflow_name,
                    session_id=new_session_id,
                    worktree_path=self.workspace,
                    ref=self.ref,
                    turn_count=result.get("num_turns", 0),
                    ttl_hours=ttl_hours,
                    streaming_token=self.job_data.get("session_token"),
                )
                logger.info(
                    f"Saved session {new_session_id[:8]}... for {self.repo}/{self.thread_type}/{self.thread_id}/{self.workflow_name}"
                )
                if self.worktree_lock:
                    await self.worktree_lock.set_session_id(new_session_id)
            except Exception as e:
                logger.warning(f"Failed to save session metadata: {e}")

        if new_session_id and self.job_data.get("session_token"):
            try:
                if self.streaming_bridge is None:
                    await self.session_service.update_session_id(
                        self.job_data["session_token"], new_session_id
                    )
                transcript_path = find_transcript_path(
                    new_session_id, self.workspace or ""
                )
                if transcript_path:
                    await self.session_service.update_transcript_path(
                        self.job_data["session_token"], transcript_path
                    )
                    write_transcript_meta(
                        transcript_path,
                        {
                            "installation_id": self.job_data.get("installation_id")
                            or self.job_data.get("event_data", {}).get(
                                "installation_id", ""
                            ),
                            "ref": self.ref,
                            "user": self.job_data.get("user", "remote-control"),
                            "thread_type": self.thread_type,
                            "conversation_config": self.job_data.get(
                                "conversation_config", ""
                            ),
                        },
                    )
            except Exception as e:
                logger.warning(f"Failed to update streaming session metadata: {e}")

    async def _mark_success(self, result):
        response = result.get("response", "")
        new_session_id = result.get("session_id")
        await self.job_queue.complete_job(
            self.job_id,
            {
                "status": "success",
                "response": response,
                "repo": self.repo,
                "issue_number": self.issue_number,
                "session_id": new_session_id,
            },
            status="success",
        )
        logger.info(f"Job {self.job_id} completed successfully")

    async def _mark_cancelled(self, result):
        response = result.get("response", "Cancelled")
        new_session_id = result.get("session_id")
        await self.job_queue.complete_job(
            self.job_id,
            {
                "status": "cancelled",
                "response": response,
                "repo": self.repo,
                "issue_number": self.issue_number,
                "session_id": new_session_id,
            },
            status="cancelled",
        )
        logger.info(f"Job {self.job_id} completed as cancelled")

    async def _handle_error(self, e: Exception):
        try:
            if self.builder is not None:
                await self.builder.flush_pending_post_jobs()
        except Exception as flush_err:
            logger.error(f"Failed to flush post-processing jobs: {flush_err}")

        if self.job_data.get("session_token"):
            try:
                from shared.session_stream import SessionStreamBridge

                await self.session_service.set_completed(
                    token=self.job_data["session_token"],
                    is_error=True,
                    repo=self.repo,
                    issue_number=self.issue_number,
                    workflow=self.workflow_name,
                    session_id=self.session_id,
                )
                err_bridge = SessionStreamBridge(
                    self.job_data["session_token"], self.job_queue.redis
                )
                await err_bridge.publish_error(str(e))
                await err_bridge.close()
                logger.info(
                    f"Marked session {self.job_data['session_token'][:8]}... as error after job failure"
                )
            except Exception as session_err:
                logger.warning(f"Failed to mark session as errored: {session_err}")

        logger.error(f"Job {self.job_id} failed: {e}", exc_info=True)

        error_type = type(e).__name__
        if isinstance(e, (WorktreeCreationError, RepositorySyncError)):
            error_category = "infrastructure"
        elif isinstance(e, (SDKError, SDKTimeoutError)):
            error_category = "sdk"
        elif isinstance(e, TimeoutError):
            error_category = "timeout"
        else:
            error_category = "execution"

        await self.job_queue.complete_job(
            self.job_id,
            {
                "status": "error",
                "error": str(e),
                "error_type": error_type,
                "error_category": error_category,
                "timestamp": time.time(),
                "repo": self.repo,
                "issue_number": self.issue_number,
            },
            status="error",
        )

    async def _cleanup(self):
        # Release session dedup lock (SETNX-based) so follow-up
        # requests can create new jobs for this session.
        if self.persist_session:
            try:
                dedup_key = session_dedup_key(
                    self.repo, self.thread_type, self.thread_id, self.workflow_name
                )
                await self.job_queue.redis.delete(dedup_key)
                logger.debug(f"Released dedup lock for {dedup_key}")
            except Exception as e:
                logger.warning(f"Failed to release dedup lock: {e}")

        if self.worktree_lock:
            try:
                await self.worktree_lock.release()
                logger.debug(f"Released worktree lock for {self.worktree_key}")
            except Exception as e:
                logger.warning(f"Failed to release worktree lock: {e}")

        try:
            if os.environ.get("GITHUB_TOKEN") == self.job_data.get("github_token"):
                del os.environ["GITHUB_TOKEN"]
                logger.debug("Cleaned up GITHUB_TOKEN from environment")
        except KeyError:
            pass

        try:
            # Keyed on job_id, so this no longer depends on self.workspace
            # being set — a job that died after configure_git but before the
            # workspace was recorded still gets its token removed.
            per_job_creds = credentials_path(self.job_id)
            if os.path.exists(per_job_creds):
                os.remove(per_job_creds)
                logger.debug("Cleaned up per-job git credentials")
                # Unset global credential helper to avoid stale references
                # when the credentials file no longer exists
                await execute_git_command(
                    ["git", "config", "--global", "--unset", "credential.helper"]
                )
        except Exception as e:
            logger.warning(f"Failed to cleanup credentials: {e}")

        if self.workspace and not self.persist_session:
            for attempt in range(3):
                try:
                    if self.repo_dir and os.path.exists(self.workspace):
                        await execute_git_command(
                            [
                                "git",
                                f"--git-dir={self.repo_dir}",
                                "worktree",
                                "remove",
                                "--force",
                                self.workspace,
                            ]
                        )
                    elif os.path.exists(self.workspace):
                        shutil.rmtree(self.workspace)
                    logger.debug(f"Cleaned up workspace: {self.workspace}")
                    break
                except Exception as e:
                    if attempt < 2:
                        logger.warning(
                            f"Failed to cleanup workspace {self.workspace} (attempt {attempt + 1}/3): {e}. Retrying..."
                        )
                        await asyncio.sleep(1)
                    else:
                        logger.error(
                            f"Failed to cleanup workspace {self.workspace} after 3 attempts: {e}",
                            exc_info=True,
                        )
        elif self.workspace and self.persist_session:
            logger.debug(
                f"Preserving persistent worktree: {self.workspace} (cleaned by TTL/event-based cleanup)"
            )
