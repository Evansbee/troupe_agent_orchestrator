"""Git plumbing: repo bootstrap, per-task worktrees, commits and merges."""

from __future__ import annotations

import re
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
        subprocess.run(["git", "worktree", "remove", "--force", str(path)], cwd=root, capture_output=True)


def diffstat(root: Path, branch: str) -> str:
    try:
        base = current_branch(root)
        return git(root, "diff", "--stat", f"{base}...{branch}")
    except GitError:
        return ""
