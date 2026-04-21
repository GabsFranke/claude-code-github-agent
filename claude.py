import argparse
import datetime
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Optional, TypedDict


class Session(TypedDict):
    session_id: str
    short_id: str
    repo: str
    thread_type: str
    thread_id: str
    workflow: str
    timestamp: datetime.datetime
    project_dir: str
    encoded_name: str


def clear_screen() -> None:
    os.system("cls" if os.name == "nt" else "clear")


def hide_cursor() -> None:
    sys.stdout.write("\033[?25l")
    sys.stdout.flush()


def show_cursor() -> None:
    sys.stdout.write("\033[?25h")
    sys.stdout.flush()


if os.name == "nt":
    import msvcrt

    def get_key() -> str:
        while True:
            if msvcrt.kbhit():
                b = msvcrt.getch()
                if b in (b"\x00", b"\xe0"):
                    b2 = msvcrt.getch()
                    if b2 == b"H":
                        return "UP"
                    if b2 == b"P":
                        return "DOWN"
                    if b2 == b"K":
                        return "LEFT"
                    if b2 == b"M":
                        return "RIGHT"
                    continue
                try:
                    c = b.decode("utf-8")
                    if c == "\r":
                        return "ENTER"
                    if c == "\x1b":
                        return "ESC"
                    return c.lower()
                except UnicodeDecodeError:
                    pass

else:
    import termios
    import tty

    def get_key() -> str:
        fd = sys.stdin.fileno()
        old_settings = termios.tcgetattr(fd)  # type: ignore
        try:
            tty.setraw(fd)  # type: ignore
            ch = sys.stdin.read(1)
            if ch == "\x1b":
                ch2 = sys.stdin.read(1)
                if ch2 == "[":
                    ch3 = sys.stdin.read(1)
                    if ch3 == "A":
                        return "UP"
                    if ch3 == "B":
                        return "DOWN"
                    if ch3 == "C":
                        return "RIGHT"
                    if ch3 == "D":
                        return "LEFT"
                return "ESC"
            if ch == "\r":
                return "ENTER"
            return ch.lower()
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)  # type: ignore


def main() -> None:
    parser = argparse.ArgumentParser(description="Resume Claude docker sessions")
    parser.add_argument("--list", action="store_true", help="Just list sessions")
    parser.add_argument("--session-id", type=str, help="Resume a specific session")
    args = parser.parse_args()

    home_dir = (
        os.environ.get("USERPROFILE")
        or os.environ.get("HOME")
        or os.path.expanduser("~")
    )
    claude_projects = Path(home_dir) / ".claude" / "projects"
    claude_worktrees = Path(home_dir) / ".claude" / "worktrees"
    claude_memory = Path(home_dir) / ".claude" / "memory"
    claude_workers = Path(home_dir) / ".claude" / "workers"

    if not claude_projects.exists():
        print(f"No Docker sessions found in {claude_projects}")
        sys.exit(0)

    docker_projects = [
        d
        for d in claude_projects.iterdir()
        if d.is_dir() and d.name.startswith("-home-bot-")
    ]

    if not docker_projects:
        print(f"No Docker sessions found in {claude_projects}")
        sys.exit(0)

    sessions: list[Session] = []
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
                            thread_id = parts[i + 1]
                            workflow = "-".join(parts[i + 2 :])
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

            sessions.append(
                {
                    "session_id": sid,
                    "short_id": sid[:8],
                    "repo": repo,
                    "thread_type": thread_type,
                    "thread_id": thread_id,
                    "workflow": workflow,
                    "timestamp": timestamp,
                    "project_dir": str(project),
                    "encoded_name": encoded,
                }
            )

    # Sort newest first initially
    sessions.sort(key=lambda x: x["timestamp"], reverse=True)

    if args.list:
        print(
            f"{'ID':<10} | {'Repo':<30} | {'Thread':<15} | {'Workflow':<20} | {'Timestamp'}"
        )
        print("-" * 100)
        for s in sessions:
            thread_disp = (
                f"{s['thread_type']}-{s['thread_id']}"
                if s["thread_type"] != "unknown"
                else "N/A"
            )
            ts_str = s["timestamp"].strftime("%Y-%m-%d %H:%M")
            print(
                f"{s['short_id']:<10} | {s['repo']:<30} | {thread_disp:<15} | {s['workflow']:<20} | {ts_str}"
            )
        sys.exit(0)

    if args.session_id:
        match = next(
            (s for s in sessions if s["session_id"].startswith(args.session_id)), None
        )
        if not match:
            print(f"\033[91mSession not found: {args.session_id}\033[0m")
            sys.exit(1)
        selected_sessions = [match]
    else:
        PAGE_SIZE = 10
        current_page = 0
        grouped = False
        selected_sessions = []
        selected_idx = 0

        hide_cursor()
        try:
            while True:
                clear_screen()
                print("\033[96mDocker Sessions:\033[0m\n")

                display_sessions = sessions.copy()
                if grouped:
                    # Group by workflow, then by timestamp
                    display_sessions.sort(
                        key=lambda x: (x["workflow"], x["timestamp"]), reverse=True
                    )
                else:
                    display_sessions.sort(key=lambda x: x["timestamp"], reverse=True)

                total_pages = max(
                    1, (len(display_sessions) + PAGE_SIZE - 1) // PAGE_SIZE
                )
                current_page = max(0, min(current_page, total_pages - 1))

                start_idx = current_page * PAGE_SIZE
                end_idx = start_idx + PAGE_SIZE
                page_sessions = display_sessions[start_idx:end_idx]

                selected_idx = max(0, min(selected_idx, len(page_sessions) - 1))

                print(
                    f"Page {current_page + 1} of {total_pages} (Total: {len(display_sessions)})"
                )
                if grouped:
                    print("\033[93m[Grouped by Workflow]\033[0m")
                print("-" * 100)

                current_workflow = None
                for i, s in enumerate(page_sessions):
                    global_idx = start_idx + i
                    if grouped and s["workflow"] != current_workflow:
                        current_workflow = s["workflow"]
                        print(f"\n\033[95m--- {current_workflow} ---\033[0m")

                    thread_disp = (
                        f"{s['thread_type']}-{s['thread_id']}"
                        if s["thread_type"] != "unknown"
                        else "N/A"
                    )
                    ts_str = s["timestamp"].strftime("%Y-%m-%d %H:%M")

                    if i == selected_idx:
                        print(
                            f"\033[7m[{global_idx:2d}] {s['short_id']} | {s['repo']:<25} | {thread_disp:<15} | {s['workflow']:<20} | {ts_str}\033[0m"
                        )
                    else:
                        print(
                            f"[{global_idx:2d}] {s['short_id']} | {s['repo']:<25} | {thread_disp:<15} | {s['workflow']:<20} | {ts_str}"
                        )

                print("-" * 100)
                print("\nOptions:")
                print("  [\u2191\u2193] Navigate rows")
                if total_pages > 1:
                    print("  [\u2190\u2192] or [n/p] Navigate pages")
                print(f"  [g] {'Disable' if grouped else 'Enable'} workflow grouping")
                print("  [Enter] Select session")
                print("  [q / Esc] Quit")
                print("")

                key = get_key()
                if key in ("q", "ESC"):
                    show_cursor()
                    print("Cancelled")
                    sys.exit(0)
                elif key == "ENTER":
                    if page_sessions:
                        selected_sessions = [page_sessions[selected_idx]]
                        break
                elif key in ("n", "RIGHT") and total_pages > 1:
                    current_page = min(total_pages - 1, current_page + 1)
                    selected_idx = 0
                elif key in ("p", "LEFT") and total_pages > 1:
                    current_page = max(0, current_page - 1)
                    selected_idx = 0
                elif key == "g":
                    grouped = not grouped
                    current_page = 0
                    selected_idx = 0
                elif key == "UP":
                    selected_idx = max(0, selected_idx - 1)
                elif key == "DOWN":
                    selected_idx = min(len(page_sessions) - 1, selected_idx + 1)
        finally:
            show_cursor()

    session = selected_sessions[0]
    print(f"\n\033[92mSelected: {session['session_id']}\033[0m")
    print(f"  Repo: {session['repo']}")
    print(f"  Workflow: {session['workflow']}")
    if session["thread_type"] != "unknown":
        print(f"  Thread: {session['thread_type']}-{session['thread_id']}")

    # Compute host path
    host_home = home_dir.replace("\\", "/")
    docker_path = re.sub(r"^-home-bot-", "", session["encoded_name"])

    if os.name == "nt":
        host_path = f"{host_home}/{docker_path}".replace("/", "\\")
        host_encoded = (
            host_path.replace(":", "-")
            .replace("\\", "-")
            .replace("/", "-")
            .replace(".", "-")
        )
    else:
        host_path = f"{host_home}/{docker_path}"
        host_encoded = host_path.replace("/", "-").replace(".", "-")

    docker_project_dir = session["project_dir"]
    host_project_dir = claude_projects / host_encoded

    print(f"\nDocker project dir: {docker_project_dir}")
    print(f"Host project dir: {host_project_dir}")

    # Create link if not exists
    if host_project_dir.exists():
        print("\033[93mHost project dir already exists\033[0m")
    else:
        print("\033[96mCreating link...\033[0m")
        if os.name == "nt":
            subprocess.run(
                [
                    "cmd",
                    "/c",
                    "mklink",
                    "/J",
                    str(host_project_dir),
                    str(docker_project_dir),
                ],
                capture_output=True,
                check=False,
            )
        else:
            os.symlink(docker_project_dir, host_project_dir)
        print(
            f"\033[92mCreated link: {host_project_dir} -> {docker_project_dir}\033[0m"
        )

    # Find worktree path
    worktree_path: Optional[Path] = None
    if session["thread_type"] in ("issue", "pr", "discussion"):
        # We know safe_repo can be reconstructed by escaping back repo parts
        safe_repo = session["repo"].replace("/", "--")
        worktree_path = (
            claude_worktrees
            / safe_repo
            / f"{session['thread_type']}-{session['thread_id']}"
            / session["workflow"]
        )
    elif session["workflow"] == "memory-extractor":
        safe_repo = session["repo"].replace("/", "\\")
        worktree_path = claude_memory / safe_repo / "memory"
    elif session["workflow"] == "retrospector":
        worktree_path = claude_workers / "retrospector"

    print("\n\033[96mTo resume this session manually, copy/paste:\033[0m")
    if worktree_path:
        if not worktree_path.exists():
            print(
                "\033[93m  # Note: The directory below is currently missing and will be created if you let me launch it for you.\033[0m"
            )
        print(f"  cd \"{worktree_path}\" ; claude --resume {session['session_id']}")
    else:
        print(f"  claude --resume {session['session_id']}")

    print("\nLaunch in a new terminal? (Y/n): ", end="", flush=True)

    launch = get_key()
    print(launch if launch in ("y", "n") else "y")

    if launch in ("y", "ENTER") or not launch:
        if worktree_path:
            if not worktree_path.exists():
                print(
                    "\033[93mCreating missing directory to allow viewing history...\033[0m"
                )
                worktree_path.mkdir(parents=True, exist_ok=True)
            print(f"\033[90mChanged to: {worktree_path}\033[0m")
            os.chdir(str(worktree_path))

        cmd_str = f"claude --resume {session['session_id']}"
        print(f"\033[92mLaunching in new terminal: {cmd_str}\033[0m")

        if os.name == "nt":
            # Launch in new PowerShell window
            os.system(f'start powershell -NoExit -Command "{cmd_str}"')
        elif sys.platform == "darwin":
            # Launch in new macOS Terminal window
            pwd = str(worktree_path) if worktree_path else os.getcwd()
            os.system(
                f"""osascript -e 'tell app "Terminal"
                do script "cd \\"{pwd}\\" && {cmd_str}"
            end tell'"""
            )
        else:
            # Linux fallback
            terminals = [
                "x-terminal-emulator",
                "gnome-terminal",
                "konsole",
                "xfce4-terminal",
            ]
            launched = False
            for term in terminals:
                if (
                    subprocess.run(
                        ["which", term], capture_output=True, check=False
                    ).returncode
                    == 0
                ):
                    if term == "gnome-terminal":
                        os.system(f'{term} -- bash -c "{cmd_str}; exec bash"')
                    else:
                        os.system(f'{term} -e "bash -c \\"{cmd_str}; exec bash\\""')
                    launched = True
                    break

            if not launched:
                print(
                    "\033[93mCould not find a terminal emulator to launch. Running in current terminal.\033[0m"
                )
                os.system(cmd_str)


if __name__ == "__main__":
    main()
