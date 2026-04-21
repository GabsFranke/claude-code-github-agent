"""YAML-driven repository setup engine."""

import asyncio
import logging
import os
import sys
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml  # type: ignore[import]
from pydantic import BaseModel, Field, ValidationError

logger = logging.getLogger(__name__)


class RepoConfig(BaseModel):
    """Configuration for a single repository."""

    setup_commands: list[str] = Field(
        default_factory=list, description="Commands to run during setup"
    )
    timeout: int = Field(
        default=300, description="Total timeout in seconds for all commands"
    )
    env: dict[str, str] | None = Field(
        default=None, description="Custom environment variables"
    )
    stop_on_failure: bool = Field(
        default=True, description="Stop executing commands if one fails"
    )


class DefaultConfig(BaseModel):
    """Default configuration for repositories not explicitly listed."""

    enabled: bool = Field(
        default=False, description="Enable setup for all repos by default"
    )
    setup_commands: list[str] = Field(
        default_factory=list, description="Default commands to run"
    )
    timeout: int = Field(default=300, description="Default timeout in seconds")
    env: dict[str, str] | None = Field(
        default=None, description="Default environment variables"
    )
    stop_on_failure: bool = Field(
        default=True, description="Stop executing commands if one fails"
    )


class RepoSetupConfig(BaseModel):
    """Root configuration containing all repository setups."""

    repositories: dict[str, RepoConfig] = Field(
        default_factory=dict, description="Map of repo names to configurations"
    )
    default: DefaultConfig = Field(
        default_factory=DefaultConfig, description="Default configuration"
    )


class RepoSetupEngine:
    """Loads repository setup configuration from YAML and executes setup commands."""

    def __init__(self, config_path: str | Path | None = None):
        """Initialize repo setup engine from YAML config.

        Args:
            config_path: Path to repo-setup.yaml file (defaults to repo-setup.yaml in project root)
        """
        if config_path is None:
            config_path = Path(__file__).parent.parent / "repo-setup.yaml"
        config_path = Path(config_path)

        if not config_path.exists():
            logger.info(
                f"Repo setup config not found: {config_path}. "
                "No repository setup will be performed."
            )
            # Initialize with empty config
            self.config = RepoSetupConfig()
            return

        try:
            with open(config_path, encoding="utf-8") as f:
                raw_config = yaml.safe_load(f)

            # Handle empty file
            if raw_config is None:
                raw_config = {}

            # Validate configuration with Pydantic
            self.config = RepoSetupConfig(**raw_config)

            logger.info(
                f"Loaded setup configuration for {len(self.config.repositories)} repositories"
            )

        except ValidationError as e:
            logger.error(f"Invalid repo setup configuration in {config_path}")
            logger.error("Validation errors:")
            for error in e.errors():
                loc = " -> ".join(str(x) for x in error["loc"])
                logger.error(f"  {loc}: {error['msg']}")
            raise ValueError(
                f"Invalid repo setup configuration: {e.error_count()} validation error(s). "
                "See logs for details."
            ) from e
        except yaml.YAMLError as e:
            logger.error(f"YAML parsing error in {config_path}: {e}")
            raise ValueError(f"Invalid YAML in repo setup configuration: {e}") from e

    def get_setup_config(self, repo: str) -> RepoConfig | None:
        """Get setup configuration for a repository.

        Args:
            repo: Repository full name (owner/repo)

        Returns:
            RepoConfig if setup is configured, None otherwise
        """
        # Check if repo is explicitly configured
        if repo in self.config.repositories:
            logger.debug(f"Found explicit setup config for {repo}")
            return self.config.repositories[repo]

        # Check if default is enabled
        if self.config.default.enabled and self.config.default.setup_commands:
            logger.debug(f"Using default setup config for {repo}")
            return RepoConfig(
                setup_commands=self.config.default.setup_commands,
                timeout=self.config.default.timeout,
                env=self.config.default.env,
                stop_on_failure=self.config.default.stop_on_failure,
            )

        # No setup configured - this is normal, don't log
        return None

    async def _run_command(
        self,
        cmd: str,
        workspace: str,
        env: dict[str, str],
        timeout: float,
    ) -> dict[str, Any]:
        """Run a single command and return its result.

        Args:
            cmd: Shell command to execute
            workspace: Working directory
            env: Environment variables
            timeout: Seconds before the command is killed

        Returns:
            Dict with command result
        """
        if sys.platform == "win32":
            shell_cmd = ["cmd.exe", "/c", cmd]
        else:
            shell_cmd = ["/bin/bash", "-c", cmd]

        try:
            process = await asyncio.create_subprocess_exec(
                *shell_cmd,
                cwd=workspace,
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

            try:
                stdout, stderr = await asyncio.wait_for(
                    process.communicate(), timeout=timeout
                )

                stdout_str = stdout.decode("utf-8", errors="replace")
                stderr_str = stderr.decode("utf-8", errors="replace")
                success = process.returncode == 0

                if success:
                    logger.info(f"  ✓ {cmd!r}")
                    if stdout_str:
                        logger.debug(f"    stdout:\n{stdout_str}")
                else:
                    logger.warning(f"  ✗ {cmd!r} (exit {process.returncode})")
                    if stdout_str:
                        logger.warning(f"    stdout (last 1000): {stdout_str[-1000:]}")
                    if stderr_str:
                        if len(stderr_str) > 2000:
                            logger.warning(
                                f"    stderr (first 1000): {stderr_str[:1000]}"
                            )
                            logger.warning(
                                f"    stderr (last 1000): {stderr_str[-1000:]}"
                            )
                        else:
                            logger.warning(f"    stderr: {stderr_str}")

                return {
                    "command": cmd,
                    "success": success,
                    "exit_code": process.returncode,
                    "stdout": stdout_str,
                    "stderr": stderr_str,
                }

            except asyncio.TimeoutError:
                logger.error(f"  ✗ {cmd!r} timed out after {timeout:.0f}s")
                try:
                    process.kill()
                    await process.wait()
                except ProcessLookupError:
                    pass
                return {
                    "command": cmd,
                    "success": False,
                    "error": "timeout",
                    "error_category": "timeout",
                    "timeout": timeout,
                }

        except OSError as e:
            # File system errors, permission issues, command not found
            logger.error(f"  ✗ {cmd!r} raised OSError: {e}", exc_info=True)
            return {
                "command": cmd,
                "success": False,
                "error": str(e),
                "error_category": "os_error",
                "error_type": "OSError",
            }
        except MemoryError as e:
            # Out of memory
            logger.error(f"  ✗ {cmd!r} raised MemoryError: {e}", exc_info=True)
            return {
                "command": cmd,
                "success": False,
                "error": str(e),
                "error_category": "resource_exhaustion",
                "error_type": "MemoryError",
            }
        except Exception as e:
            # Catch-all for unexpected errors
            logger.error(f"  ✗ {cmd!r} raised exception: {e}", exc_info=True)
            return {
                "command": cmd,
                "success": False,
                "error": str(e),
                "error_category": "unknown",
                "error_type": type(e).__name__,
            }

    async def run_setup(
        self, workspace: str, repo: str, config: RepoConfig
    ) -> dict[str, Any]:
        """Execute setup commands in workspace, one command at a time.

        Each command runs in its own shell process with its own result.
        The total timeout budget is shared across all commands.

        Args:
            workspace: Path to workspace directory
            repo: Repository name (for logging)
            config: Setup configuration

        Returns:
            Dict with execution results
        """
        if not config.setup_commands:
            logger.info(f"No setup commands configured for {repo}")
            return {
                "completed": True,
                "results": [],
                "all_successful": True,
                "skipped": True,
            }

        logger.info(
            f"Running {len(config.setup_commands)} setup command(s) for {repo} "
            f"(timeout: {config.timeout}s, stop_on_failure: {config.stop_on_failure})"
        )

        # Prepare environment
        env = os.environ.copy()
        if config.env:
            for key, value in config.env.items():
                if key == "PATH" and "$PATH" in value:
                    current_path = env.get("PATH", "")
                    env[key] = value.replace("$PATH", current_path)
                else:
                    env[key] = value
            logger.debug(f"Added custom env vars: {list(config.env.keys())}")

        start_time = asyncio.get_event_loop().time()
        results: list[dict[str, Any]] = []
        all_successful = True

        for cmd in config.setup_commands:
            elapsed = asyncio.get_event_loop().time() - start_time
            remaining = config.timeout - elapsed

            if remaining <= 0:
                logger.error(f"✗ Timeout budget exhausted before running: {cmd!r}")
                results.append({"command": cmd, "success": False, "error": "timeout"})
                all_successful = False
                break

            result = await self._run_command(cmd, workspace, env, remaining)
            results.append(result)

            if not result["success"]:
                all_successful = False
                if config.stop_on_failure:
                    skipped = config.setup_commands[len(results) :]
                    if skipped:
                        logger.warning(
                            f"Skipping {len(skipped)} remaining command(s) due to failure"
                        )
                    break

        elapsed = asyncio.get_event_loop().time() - start_time

        if all_successful:
            logger.info(f"✓ Setup completed successfully for {repo} in {elapsed:.1f}s")
        else:
            logger.warning(
                f"✗ Setup completed with failures for {repo} in {elapsed:.1f}s"
            )

        return {
            "completed": True,
            "results": results,
            "all_successful": all_successful,
            "elapsed_seconds": elapsed,
            "skipped": False,
        }

    def list_configured_repos(self) -> list[str]:
        """List all repositories with explicit setup configuration.

        Returns:
            List of repository names
        """
        return list(self.config.repositories.keys())


@lru_cache(maxsize=1)
def get_repo_setup_engine(config_path: str | None = None) -> RepoSetupEngine:
    """Get cached RepoSetupEngine instance (singleton per config path).

    The engine is cached to avoid repeatedly parsing and validating repo-setup.yaml.
    Since repository setup configuration is static during runtime, caching provides
    performance benefits with no downsides.

    Args:
        config_path: Path to repo-setup.yaml file (defaults to repo-setup.yaml in project root)

    Returns:
        Cached RepoSetupEngine instance

    Note:
        Changes to repo-setup.yaml require a process restart to take effect.
    """
    return RepoSetupEngine(config_path)
