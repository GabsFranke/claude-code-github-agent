import argparse
import datetime
import os
import re
import subprocess
import sys
from pathlib import Path

def main():
    parser = argparse.ArgumentParser(description="Resume Claude docker sessions")
    parser.add_argument("--list", action="store_true", help="Just list sessions")
    parser.add_argument("--session-id", type=str, help="Resume a specific session")
    args = parser.parse_args()

    user_profile = os.environ.get("USERPROFILE", os.path.expanduser("~"))
    claude_projects = Path(user_profile) / ".claude" / "projects"
    claude_worktrees = Path(user_profile) / ".claude" / "worktrees"
    claude_memory = Path(user_profile) / ".claude" / "memory"
    claude_workers = Path(user_profile) / ".claude" / "workers"

    if not claude_projects.exists():
        print(f"No Docker sessions found in {claude_projects}")
        sys.exit(0)

    docker_projects = [d for d in claude_projects.iterdir() if d.is_dir() and d.name.startswith("-home-bot-")]

    if not docker_projects:
        print(f"No Docker sessions found in {claude_projects}")
        sys.exit(0)

    sessions = []
    for project in docker_projects:
        for f in project.glob("*.jsonl"):
            sid = f.stem
            encoded = project.name

            repo = "unknown"
            workflow = "unknown"
            thread_type = "unknown"
            thread_id = "unknown"

            # Parse encoded path
            m = re.match(r"-home-bot--claude-worktrees-(.+)$", encoded)
            if m:
                parts = m.group(1).split("-")
                if len(parts) >= 4:
                    for i, part in enumerate(parts):
                        if part in ("issue", "pr", "discussion"):
                            thread_type = part
                            thread_id = parts[i+1]
                            workflow = "-".join(parts[i+2:])
                            raw_repo = "-".join(parts[:i])
                            repo = raw_repo.replace("--", "/")
                            break
            else:
                m = re.match(r"-home-bot--claude-memory-([^-]+)-(.+)-memory$", encoded)
                if m:
                    repo = f"{m.group(1)}/{m.group(2)}"
                    workflow = "memory-extractor"
                else:
                    m = re.match(r"-home-bot--claude-workers-retrospector", encoded)
                    if m:
                        repo = "bot-repo"
                        workflow = "retrospector"

            timestamp = datetime.datetime.fromtimestamp(f.stat().st_mtime)

            sessions.append({
                "session_id": sid,
                "short_id": sid[:8],
                "repo": repo,
                "thread_type": thread_type,
                "thread_id": thread_id,
                "workflow": workflow,
                "timestamp": timestamp,
                "project_dir": str(project),
                "encoded_name": encoded
            })

    # Sort newest first
    sessions.sort(key=lambda x: x["timestamp"], reverse=True)

    if args.list:
        print(f"{'ID':<10} | {'Repo':<30} | {'Thread':<15} | {'Workflow':<20} | {'Timestamp'}")
        print("-" * 100)
        for s in sessions:
            thread_disp = f"{s['thread_type']}-{s['thread_id']}" if s['thread_type'] != "unknown" else "N/A"
            ts_str = s['timestamp'].strftime('%Y-%m-%d %H:%M')
            print(f"{s['short_id']:<10} | {s['repo']:<30} | {thread_disp:<15} | {s['workflow']:<20} | {ts_str}")
        sys.exit(0)

    if args.session_id:
        match = next((s for s in sessions if s["session_id"].startswith(args.session_id)), None)
        if not match:
            print(f"\033[91mSession not found: {args.session_id}\033[0m")
            sys.exit(1)
        sessions = [match]
    else:
        print("\n\033[96mDocker Sessions:\033[0m\n")
        for i, s in enumerate(sessions):
            thread_disp = f"{s['thread_type']}-{s['thread_id']}" if s['thread_type'] != "unknown" else "N/A"
            ts_str = s['timestamp'].strftime('%Y-%m-%d %H:%M')
            print(f"[{i}] {s['short_id']} | {s['repo']} | {thread_disp} | {s['workflow']} | {ts_str}")

        print("")
        try:
            choice = input(f"Pick a session (0-{len(sessions)-1}) or Enter to cancel: ").strip()
        except KeyboardInterrupt:
            print("\nCancelled")
            sys.exit(0)

        if not choice or not choice.isdigit():
            print("Cancelled")
            sys.exit(0)

        idx = int(choice)
        if idx < 0 or idx >= len(sessions):
            print("\033[91mInvalid choice\033[0m")
            sys.exit(1)

        sessions = [sessions[idx]]

    session = sessions[0]
    print(f"\n\033[92mSelected: {session['session_id']}\033[0m")
    print(f"  Repo: {session['repo']}")
    print(f"  Workflow: {session['workflow']}")
    if session['thread_type'] != "unknown":
        print(f"  Thread: {session['thread_type']}-{session['thread_id']}")

    # Compute Windows path
    host_home = user_profile.replace("\\", "/")
    docker_path = re.sub(r"^-home-bot-", "", session['encoded_name'])
    windows_path = f"{host_home}/{docker_path}".replace("/", "\\")

    windows_encoded = windows_path.replace(":", "-").replace("\\", "-").replace("/", "-").replace(".", "-")

    docker_project_dir = session['project_dir']
    windows_project_dir = claude_projects / windows_encoded

    print(f"\nDocker project dir: {docker_project_dir}")
    print(f"Windows project dir: {windows_project_dir}")

    # Create junction if not exists
    if windows_project_dir.exists():
        print("\033[93mWindows project dir already exists (junction)\033[0m")
    else:
        print("\033[96mCreating junction...\033[0m")
        subprocess.run(["cmd", "/c", "mklink", "/J", str(windows_project_dir), str(docker_project_dir)], capture_output=True)
        print(f"\033[92mCreated junction: {windows_project_dir} -> {docker_project_dir}\033[0m")

    # Find worktree path
    worktree_path = None
    if session['thread_type'] in ("issue", "pr", "discussion"):
        # We know safe_repo can be reconstructed by escaping back repo parts
        safe_repo = session['repo'].replace("/", "--")
        worktree_path = claude_worktrees / safe_repo / f"{session['thread_type']}-{session['thread_id']}" / session['workflow']
    elif session['workflow'] == "memory-extractor":
        safe_repo = session['repo'].replace("/", "\\")
        worktree_path = claude_memory / safe_repo / "memory"
    elif session['workflow'] == "retrospector":
        worktree_path = claude_workers / "retrospector"

    print("\n\033[96mTo resume this session manually, copy/paste:\033[0m")
    if worktree_path:
        if not worktree_path.exists():
            print(f"\033[93m  # Note: The directory below is currently missing and will be created if you let me launch it for you.\033[0m")
        print(f"  cd \"{worktree_path}\" ; claude --resume {session['session_id']}")
    else:
        print(f"  claude --resume {session['session_id']}")

    print("\nLaunch in a new terminal? (Y/n): ", end="", flush=True)
    try:
        launch = input().strip().lower()
    except KeyboardInterrupt:
        print("\nCancelled")
        sys.exit(0)

    if not launch or launch == "y":
        if worktree_path:
            if not worktree_path.exists():
                print("\033[93mCreating missing directory to allow viewing history...\033[0m")
                worktree_path.mkdir(parents=True, exist_ok=True)
            print(f"\033[90mChanged to: {worktree_path}\033[0m")
            os.chdir(str(worktree_path))

        cmd_str = f"claude --resume {session['session_id']}"
        print(f"\033[92mLaunching in new terminal: {cmd_str}\033[0m")

        # Launch in new PowerShell window
        os.system(f'start powershell -NoExit -Command "{cmd_str}"')

if __name__ == "__main__":
    main()
