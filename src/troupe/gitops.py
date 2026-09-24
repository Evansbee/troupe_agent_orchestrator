"""Git plumbing: repo bootstrap, per-task worktrees, commits and merges."""

from __future__ import annotations

import re
import os
import signal
import time
import subprocess
import threading
from pathlib import Path

# Serializes operations on the main checkout across engine threads.
MAIN_LOCK = threading.Lock()

GITIGNORE_LINES = [".troupe/", ".DS_Store", "__pycache__/", ".venv/", "node_modules/"]


class GitError(RuntimeError):
    pass


def git(cwd: Path | str, *args: str, check: bool = True) -> str:
    p = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True)
    if check and p.returncode != 0:
        raise GitError(f"git {' '.join(args)} failed: {(p.stderr or p.stdout).strip()}")
    return p.stdout.strip()


def is_repo(root: Path) -> bool:
    return subprocess.run(["git", "rev-parse", "--git-dir"], cwd=root, capture_output=True).returncode == 0


def has_commits(root: Path) -> bool:
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, capture_output=True).returncode == 0


def ensure_repo(root: Path) -> None:
    if not is_repo(root):
        git(root, "init", "-b", "main")
    gi = root / ".gitignore"
    existing = gi.read_text().splitlines() if gi.exists() else []
    missing = [line for line in GITIGNORE_LINES if line not in existing]
    if missing:
        gi.write_text("\n".join(existing + missing).strip() + "\n")
    if not has_commits(root):
        git(root, "add", "-A")
        git(root, "commit", "--allow-empty", "-m", "troupe: initial commit")


def current_branch(root: Path) -> str:
    return git(root, "rev-parse", "--abbrev-ref", "HEAD")


def slug(text: str, n: int = 32) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s[:n].strip("-") or "task"


def create_worktree(root: Path, worktrees_dir: Path, task_id: int, title: str) -> tuple[str, Path]:
    branch = f"troupe/t{task_id}-{slug(title)}"
    path = worktrees_dir / f"t{task_id}"
    with MAIN_LOCK:
        if path.exists():
            return branch, path
        worktrees_dir.mkdir(parents=True, exist_ok=True)
        base = current_branch(root)
        exists = subprocess.run(["git", "rev-parse", "--verify", branch], cwd=root, capture_output=True).returncode == 0
        if exists:
            git(root, "worktree", "add", str(path), branch)
        else:
            git(root, "worktree", "add", "-b", branch, str(path), base)
    return branch, path


def commit_all(cwd: Path, message: str) -> bool:
    """Stage and commit everything; returns True if a commit was made."""
    git(cwd, "add", "-A")
    if not git(cwd, "status", "--porcelain"):
        return False
    git(cwd, "commit", "-m", message, "--no-verify")
    return True


def autocommit_main(root: Path, message: str) -> bool:
    with MAIN_LOCK:
        if (root / ".git" / "MERGE_HEAD").exists():
            return False
        try:
            return commit_all(root, message)
        except GitError:
            return False


def merge_branch(root: Path, branch: str, message: str) -> tuple[bool, str]:
    """Merge a task branch into the main checkout. On conflict, abort and report."""
    with MAIN_LOCK:
        try:
            commit_all(root, "troupe: snapshot before merge")
        except GitError:
            pass
        p = subprocess.run(["git", "merge", "--no-ff", "-m", message, branch], cwd=root,
                           capture_output=True, text=True)
        if p.returncode == 0:
            return True, p.stdout.strip()
        subprocess.run(["git", "merge", "--abort"], cwd=root, capture_output=True)
        return False, (p.stdout + p.stderr).strip()


def remove_worktree(root: Path, path: Path) -> None:
    with MAIN_LOCK:
        git(root, "worktree", "remove", "--force", str(path))


def diffstat(root: Path, branch: str) -> str:
    try:
        base = current_branch(root)
        return git(root, "diff", "--stat", f"{base}...{branch}")
    except GitError:
        return ""


def delete_branch(root: Path, branch: str) -> None:
    """Delete only a branch Git confirms is merged."""
    with MAIN_LOCK:
        git(root, "branch", "-d", branch)


def task_worktrees(root: Path, directory: Path) -> list[tuple[int, Path]]:
    """Return registered task worktrees directly inside the managed directory."""
    records = git(root, "worktree", "list", "--porcelain", "-z").split("\0")
    out = []
    for record in records:
        if not record.startswith("worktree "):
            continue
        path = Path(record[len("worktree "):])
        match = re.fullmatch(r"t(\d+)", path.name)
        if match and path.parent.resolve() == directory.resolve() and not path.is_symlink():
            out.append((int(match[1]), path))
    return out


def prune_worktrees(root: Path) -> None:
    with MAIN_LOCK:
        git(root, "worktree", "prune")


def prepare_check(root: Path, tree: Path, branch: str) -> tuple[str, str]:
    """Bring main into the task tree and return the exact heads being checked."""
    with MAIN_LOCK:
        if current_branch(tree) != branch:
            raise GitError("Task worktree is not on its recorded branch")
        if git(tree, "status", "--porcelain"):
            raise GitError("Task worktree has uncommitted changes; commit and resubmit for review")
        main_head = git(root, "rev-parse", "HEAD")
        try:
            git(tree, "merge", "--no-edit", main_head)
        except GitError:
            git(tree, "merge", "--abort", check=False)
            raise
        return main_head, git(tree, "rev-parse", "HEAD")


def run_check(tree: Path, command: str, timeout: float, log_path: Path,
              stop: threading.Event) -> tuple[bool, str]:
    """Capture output without buffering it in memory; stop the whole process group."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    outcome = ""
    with log_path.open("w") as log:
        log.write(f"$ {command}\n")
        log.flush()
        if stop.is_set():
            log.write("Check stopped\n")
            return False, "Check stopped"
        try:
            proc = subprocess.Popen(command, shell=True, cwd=tree, stdout=log, stderr=subprocess.STDOUT,
                                    start_new_session=True)
            deadline = time.monotonic() + timeout
            while proc.poll() is None:
                if stop.is_set() or time.monotonic() >= deadline:
                    outcome = "Check stopped" if stop.is_set() else f"Check timed out after {timeout:g}s"
                    try:
                        os.killpg(proc.pid, signal.SIGTERM)
                        proc.wait(timeout=1)
                    except subprocess.TimeoutExpired:
                        os.killpg(proc.pid, signal.SIGKILL)
                        proc.wait()
                    except ProcessLookupError:
                        proc.wait()
                    # The shell can exit before a descendant that ignores SIGTERM.
                    try:
                        os.killpg(proc.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    break
                stop.wait(0.05)
            if not outcome:
                outcome = f"Check exited {proc.returncode}"
            passed = proc.returncode == 0 and outcome == "Check exited 0" and not stop.is_set()
        except OSError as e:
            outcome, passed = f"Could not start check: {e}", False
        log.write(f"\n{outcome}\n")
    return passed, outcome


def merge_checked(root: Path, tree: Path, branch: str, main_head: str, task_head: str,
                  message: str, stop: threading.Event | None = None) -> tuple[bool, str]:
    """Never merge code different from the checked tree, or silently change main first."""
    with MAIN_LOCK:
        if stop is not None and stop.is_set():
            return False, "Check stopped"
        if (git(root, "rev-parse", "HEAD") != main_head or git(root, "status", "--porcelain")
                or git(tree, "rev-parse", "HEAD") != task_head or git(tree, "status", "--porcelain")
                or git(root, "rev-parse", branch) != task_head):
            return False, "Repository changed during checks; resubmit so the current tree is checked."
        p = subprocess.run(["git", "merge", "--no-ff", "-m", message, task_head], cwd=root,
                           capture_output=True, text=True)
        if p.returncode:
            git(root, "merge", "--abort", check=False)
        return p.returncode == 0, (p.stdout + p.stderr).strip()
