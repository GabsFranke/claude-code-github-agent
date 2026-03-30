"""YAML-driven repository setup engine."""

import asyncio
import logging
import os
from pathlib import Path
from typing import Any

import yaml  # type: ignore[import-untyped]
from pydantic import BaseModel, Field, ValidationError

logger = logging.getLogger(__name__)


class RepoConfig(BaseModel):
    """Configuration for a single repository."""

    setup_commands: list[str] = Field(
        default_factory=list, description="Commands to run during setup"
    )
    timeout: int = Field(default=300, description="Timeout in seconds for all commands")
    env: dict[str, str] | None = Field(
        default=None, description="Custom environment variables"
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
            )

        # No setup configured - this is normal, don't log
        return None

    async def run_setup(
        self, workspace: str, repo: str, config: RepoConfig
    ) -> dict[str, Any]:
        """Execute setup commands in workspace.

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
            f"(timeout: {config.timeout}s)"
        )

        results = []
        start_time = asyncio.get_event_loop().time()

        # Prepare environment
        env = os.environ.copy()
        if config.env:
            env.update(config.env)
            logger.debug(f"Added custom env vars: {list(config.env.keys())}")

        for idx, cmd in enumerate(config.setup_commands, 1):
            logger.info(f"[{idx}/{len(config.setup_commands)}] Running: {cmd}")

            try:
                # Create subprocess
                process = await asyncio.create_subprocess_shell(
                    cmd,
                    cwd=workspace,
                    env=env,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )

                # Wait with timeout
                try:
                    stdout, stderr = await asyncio.wait_for(
                        process.communicate(), timeout=config.timeout
                    )

                    stdout_str = stdout.decode("utf-8", errors="replace")
                    stderr_str = stderr.decode("utf-8", errors="replace")

                    success = process.returncode == 0

                    if success:
                        logger.info(f"[{idx}/{len(config.setup_commands)}] ✓ Success")
                    else:
                        logger.warning(
                            f"[{idx}/{len(config.setup_commands)}] ✗ Failed with code {process.returncode}"
                        )
                        if stderr_str:
                            logger.warning(f"stderr: {stderr_str[:500]}")

                    results.append(
                        {
                            "command": cmd,
                            "success": success,
                            "exit_code": process.returncode,
                            "stdout": stdout_str,
                            "stderr": stderr_str,
                        }
                    )

                    # Stop on first failure
                    if not success:
                        logger.warning(
                            f"Setup command failed, skipping remaining commands for {repo}"
                        )
                        break

                except asyncio.TimeoutError:
                    logger.error(
                        f"[{idx}/{len(config.setup_commands)}] ✗ Timeout after {config.timeout}s"
                    )
                    # Kill the process
                    try:
                        process.kill()
                        await process.wait()
                    except ProcessLookupError:
                        pass

                    results.append(
                        {
                            "command": cmd,
                            "success": False,
                            "error": "timeout",
                            "timeout": config.timeout,
                        }
                    )
                    break  # Don't continue after timeout

            except Exception as e:
                logger.error(
                    f"[{idx}/{len(config.setup_commands)}] ✗ Exception: {e}",
                    exc_info=True,
                )
                results.append({"command": cmd, "success": False, "error": str(e)})
                break  # Don't continue after exception

        elapsed = asyncio.get_event_loop().time() - start_time
        all_successful = all(r.get("success", False) for r in results)

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
