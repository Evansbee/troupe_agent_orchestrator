"""Human approval of protected changes, bound to the exact reviewed content."""
from __future__ import annotations

import json
import subprocess
import time
from collections import deque
from pathlib import Path

from . import gitops
from .safety import audit, fingerprint, protected, redact
from .store import now

MERGE_RETRY_ATTEMPTS = 3  # #81: how many times to re-check after "repository changed during checks"
MERGE_RETRY_BACKOFF_SECONDS = 2.0  # wait between retries, so main has a moment to settle
LAUNCH_SMOKE_PREFIXES = ("src/troupe/gui/", "src/troupe/tui/")
LAUNCH_SMOKE_TIMEOUT = 90  # scripts/launch_smoke.py itself budgets well under 60s
MERGE_REQUEUE_BACKOFF_SECONDS = 30.0  # #107: real code movement exhausted retries — sit out a
                                       # while before spending another full check on the same task


def needs_launch_smoke(files: list[str]) -> bool:
    return any(f.startswith(LAUNCH_SMOKE_PREFIXES) for f in files)


def run_launch_smoke(tree: Path, log_path: Path) -> str:
    """#92/REQ-ENG-059: any change touching gui/ or tui/ must actually launch the client, because
    the merge gate's normal `cfg.git.check` (pytest) never opens a real window. Appends to the same
    check log as the main check. Returns "" on a pass; a skip is logged but also returns "" (a skip
    is not a failure, but it must never be silently indistinguishable from a real pass); anything
    else is a real failure that bounces the task like a failed check.

    QA's #92 review, blocker 1: this must run the TASK TREE's own code through its own venv --
    `sys.executable` here is the *engine's* interpreter (e.g. an installed troupe's own python),
    which imports whatever's installed there, not this tree's code; a `uv run` in the tree (same as
    `cfg.git.check`'s own "uv run pytest") is what actually picks up the tree's own dependencies and
    its own src/troupe. A tree whose gui/app.py can't even import must fail this, not silently pass
    by running someone else's copy."""
    script = tree / "scripts" / "launch_smoke.py"
    command = ["uv", "run", "python", "scripts/launch_smoke.py"]
    with log_path.open("a") as log:
        log.write(f"\n$ {' '.join(command)}  (cwd={tree}, gui/tui change)\n")
        if not script.exists():
            log.write("launch smoke not run: scripts/launch_smoke.py missing from this tree\n")
            return ""
        try:
            p = subprocess.run(command, cwd=tree, capture_output=True,
                               text=True, timeout=LAUNCH_SMOKE_TIMEOUT)
        except subprocess.TimeoutExpired:
            log.write(f"launch smoke timed out after {LAUNCH_SMOKE_TIMEOUT:g}s\n")
            return f"launch smoke timed out after {LAUNCH_SMOKE_TIMEOUT:g}s"
        log.write(p.stdout)
        log.write(p.stderr)
        if "SKIP:" in p.stdout:
            reason = next((l for l in p.stdout.splitlines() if l.startswith("SKIP:")), "SKIP: unknown")
            log.write(f"launch smoke not run: {reason}\n")
            return ""
        if p.returncode != 0:
            return f"launch smoke failed (exit {p.returncode}):\n{p.stderr[-4000:]}"
        return ""


def request(store, key: str, title: str, context: str, payload: dict, task_id=None) -> dict:
    previous = store.kv_get(key)
    digest = fingerprint(json.dumps(payload, sort_keys=True))
    if previous and previous['digest'] == digest:
        return previous
    if previous:
        store.x("UPDATE questions SET status='dismissed' WHERE id=? AND status='open'", previous['qid'])
    qid = store.ask('system', title, redact(context), ['Approve', 'Reject'], task_id=task_id, kind='safety')
    record = dict(qid=qid, digest=digest, payload=payload)
    store.kv_set(key, record)
    audit(store, title)
    return record


def verdict(store, record: dict) -> str:
    q = store.one('SELECT * FROM questions WHERE id=?', record['qid'])
    if not q or q['status'] == 'open':
        return ''
    return 'approve' if q['status'] == 'answered' and q['answer'].split(' — ', 1)[0].strip().lower() == 'approve' else 'reject'


def guard_config(cfg) -> None:
    from .store import Store
    s = Store(cfg.db_path)
    # doc_only_paths (#107) lets a main commit skip the merge-gate re-check entirely, so — same as
    # `check` — it's part of what the human approves here, not read straight off troupe.toml
    # (config.py already rejects the worst patterns at load time, but ANY change still needs
    # sign-off, same as narrowing/widening `protected`). Existing installs approved a baseline
    # without this key, so its first appearance here mismatches and raises one re-approval card.
    desired = {'safety': cfg.safety, 'check': cfg.git.check, 'check_timeout': cfg.git.check_timeout,
               'doc_only_paths': cfg.git.doc_only_paths}
    approved = s.kv_get('safety.approved')
    if desired != approved:
        record = request(s, 'safety.config', 'Approve safety / merge-check configuration?',
                         'Approved:\n' + json.dumps(approved, indent=2) + '\nProposed:\n' + json.dumps(desired, indent=2), desired)
        if verdict(s, record) == 'approve':
            approved = desired
            s.kv_set('safety.approved', approved)
            s.kv_set('safety.approved_hash', record['digest'])
            audit(s, 'Human approved safety configuration', notify=False)
    if approved is None:
        from .safety import PROTECTED
        # No approval yet (still pending, or rejected): doc_only_paths defaults to empty here,
        # not config.py's normal default — the merge-gate exception must never be active before
        # the human has actually approved it, even the "standard" list.
        approved = {'safety': {'protected': list(PROTECTED), 'remotes': [], 'secret_allow': [], 'roles': {}},
                    'check': '', 'check_timeout': 600, 'doc_only_paths': []}
    cfg.safety = approved['safety']
    cfg.git.check, cfg.git.check_timeout = approved['check'], approved['check_timeout']
    # `.get(...)` covers a pre-#107 approved baseline that predates this key (same reasoning as
    # the `approved is None` branch above: no explicit approval of the exception means it's off).
    cfg.git.doc_only_paths = approved.get('doc_only_paths', [])


def task_gate(cfg, store, task: dict) -> bool:
    if store.kv_get('safety.approved') is None:
        store.update_task(task['id'], status='review', review_notes='Awaiting human approval of safety baseline')
        store.kv_set(f'safety.baseline_wait.{task["id"]}', True)
        return False
    if not task['branch']:
        return True
    base = gitops.git(cfg.root, 'merge-base', 'HEAD', task['branch'])
    head = gitops.git(cfg.root, 'rev-parse', task['branch'])
    files = gitops.git(cfg.root, 'diff', '--no-renames', '--name-only', base, head).splitlines()
    guarded = [f for f in files if protected(f, cfg.safety)]
    if not guarded:
        return True
    # Restricted to the guarded paths themselves, not the whole base..head patch (#107 scope
    # addition, per the lead's decision after #96 re-asked following a clean re-merge). `base` is
    # `merge-base(HEAD, branch)`, and both the merge gate's own retry loop
    # (_check_and_merge_with_retry, re-merging main into the branch on every attempt) and a
    # builder's own manual `git merge main` after a conflict can advance it. A whole-patch
    # fingerprint would pick up any non-protected content that happened to differ between an old
    # and new base too; restricting to the guarded paths makes the fingerprint depend only on
    # those files' content in `base` and `head`, immune to base movement that never touches them,
    # while still catching a real edit to a protected file — including one introduced by
    # conflict-resolution while merging main in. (Tried several ways to reproduce #96's exact
    # spurious re-ask against the old whole-patch code — repeated and multi-file main advances
    # merged cleanly into the branch — and couldn't get it to fire; the old code held up in every
    # scenario tried. This change is still strictly more precise than the old one and directly
    # matches what was asked for, but flagging honestly that the specific historical repro wasn't
    # reproduced here.)
    patch = gitops.git(cfg.root, 'diff', '--no-ext-diff', base, head, '--', *guarded)
    diff_path = cfg.state_dir / 'pending' / f't{task["id"]}-{head}.diff'
    diff_path.parent.mkdir(parents=True, exist_ok=True)
    diff_path.write_text(redact(patch))
    stat = gitops.git(cfg.root, 'diff', '--numstat', base, head, '--', *guarded)
    payload = {'policy': cfg.safety, 'diff': fingerprint(patch)}
    record = request(store, f'safety.task.{task["id"]}', f'Approve protected changes in #{task["id"]}?',
                     'Protected files:\n' + '\n'.join(guarded) + '\nAdded / removed lines:\n' + stat
                     + f'\nFull diff: {diff_path}\n\n{task["result"]}', payload, task['id'])
    choice = verdict(store, record)
    if choice == 'approve':
        return True
    if choice == 'reject':
        q = store.one('SELECT * FROM questions WHERE id=?', record['qid'])
        note = 'Human rejected protected changes: ' + (q['answer'] or 'Rejected')
        store.update_task(task['id'], status='in_progress', review_notes=note, next_attempt_at=0)
        store.kv_set(f'safety.waiting.{task["id"]}', False)
        store.send('system', task['assignee'], note, task_id=task['id'])
        audit(store, f'Human rejected protected task #{task["id"]}', notify=False)
    else:
        store.update_task(task['id'], status='review', review_notes='Awaiting human approval of protected changes')
        store.kv_set(f'safety.waiting.{task["id"]}', True)
    return False


def process_answers(cfg, store) -> None:
    for task in store.tasks(('review',)):
        if store.kv_get('safety.approved') and store.kv_get(f'safety.baseline_wait.{task["id"]}'):
            store.kv_set(f'safety.baseline_wait.{task["id"]}', False)
            store.update_task(task['id'], status='approved')
            continue
        key = f'safety.task.{task["id"]}'
        record = store.kv_get(key)
        if store.kv_get(f'safety.waiting.{task["id"]}') and record and verdict(store, record):
            if task_gate(cfg, store, task):
                store.update_task(task['id'], status='approved')
                store.kv_set(f'safety.waiting.{task["id"]}', False)
                audit(store, f'Human approved protected task #{task["id"]}', notify=False)
    for row in store.q("SELECT key,value FROM kv WHERE key LIKE 'safety.patch.%'"):
        record = json.loads(row['value'])
        if record.get('handled') or not verdict(store, record):
            continue
        data = record['payload']
        if verdict(store, record) == 'approve':
            path = Path(data['path'])
            if fingerprint(path.read_text()) != data['hash']:
                audit(store, 'Held patch changed; refusing approval')
                continue
            try:
                with gitops.MAIN_LOCK:
                    gitops.git(cfg.root, 'apply', '--check', str(path))
                    gitops.git(cfg.root, 'apply', '--index', str(path))
                    from .safety import scan_staged
                    scan_staged(cfg.root)
                    gitops.git(cfg.root, 'commit', '-m', 'Human-approved protected changes', '--only', '--', *data['paths'])
            except gitops.GitError as e:
                audit(store, f'Could not apply approved patch: {e}')
                continue
        else:
            store.send('system', data['author'], 'Human rejected held protected changes.', subject='Protected changes rejected')
        record['handled'] = True
        store.kv_set(row['key'], record)
        audit(store, 'Human resolved held protected edits', notify=False)


def hold_main(cfg, store, author: str, run_id: int) -> None:
    """Hold only protected paths; leave unrelated edits for normal auto-commit."""
    with gitops.MAIN_LOCK:
        tracked = gitops.git(cfg.root, 'diff', '--name-only', 'HEAD').splitlines()
        new = gitops.git(cfg.root, 'ls-files', '--others', '--exclude-standard').splitlines()
        paths = [p for p in tracked + new if protected(p, cfg.safety)]
        if not paths:
            return
        gitops.git(cfg.root, 'add', '--', *paths)
        from .safety import scan_staged
        scan_staged(cfg.root)
        patch = gitops.git(cfg.root, 'diff', '--cached', '--binary', 'HEAD', '--', *paths) + '\n'
        path = cfg.state_dir / 'pending' / f'{run_id}.patch'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(patch)
        request(store, f'safety.patch.{run_id}', 'Approve protected edits made in main?',
                '\n'.join(paths) + '\nAdded / removed lines:\n' + gitops.git(cfg.root, 'diff', '--cached', '--numstat', '--', *paths) + f'\nFull patch: {path}',
                {'path': str(path), 'hash': fingerprint(patch), 'author': author, 'paths': paths})
        old = [p for p in paths if p not in new]
        if old:
            gitops.git(cfg.root, 'restore', '--source=HEAD', '--staged', '--worktree', '--', *old)
        for p in paths:
            if p in new:
                gitops.git(cfg.root, 'rm', '--cached', '--', p)
                (cfg.root / p).unlink()


class CheckStop:
    def __init__(self, engine):
        self.engine = engine

    def is_set(self):
        return self.engine._stop.is_set() or bool(self.engine.store.kv_get("stopped"))

    def wait(self, seconds):
        self.engine._stop.wait(seconds)
        return self.is_set()


class MergeGateMixin:
    def process_approved(self) -> None:
        if not self._merge_lock.acquire(blocking=False):
            return
        try:
            self._process_approved()
        finally:
            self.store.kv_set("checking_task", None)
            self._merge_lock.release()

    def check_failed(self, task: dict, output: str) -> None:
        key = f"check_failures.{task['id']}"
        self.store.kv_set(key, (self.store.kv_get(key, 0) or 0) + 1)
        note = f"Checks failed on #{task['id']}:\n{output}"
        self.store.update_task(task["id"], actor="system", status="in_progress", next_attempt_at=0,
                               review_notes=note, event_text=f"Checks failed on #{task['id']}")
        self.store.kv_set(f"check_failed.{task['id']}", True)
        if task["assignee"]:
            self.store.send("system", task["assignee"], note + "\nFix the failure and call complete_task for QA review.",
                            subject=f"#{task['id']} checks failed", task_id=task["id"], kind="system")

    def _check_and_merge_with_retry(self, cfg, s, t: dict, tree: Path, log_path: Path) -> tuple[bool, str, bool]:
        """Run check + merge, retrying only on "repository changed during checks" (#81) — main moves
        constantly from non-builder autocommits, and that's not the builder's fault. Re-merges main
        into the task tree and re-runs the check fresh each attempt, so the tree that finally merges
        is always the one that just passed. A real check failure or a genuine merge conflict still
        bounces to the builder immediately, with no retry — retrying wouldn't change the outcome.

        `merge_checked` gets `cfg.git.doc_only_paths` (#107): if main only moved via commits that
        can't touch what the check verifies (docs/specs by default), that alone doesn't count as
        "changed" and the merge lands on the first attempt without a retry at all.

        If all retries genuinely lose the race to real code movement, this no longer bounces the
        task to the builder (#107) — an approved task didn't get worse because main moved, so it
        shouldn't cost a builder/QA cycle. It stays `approved` and is requeued with a backoff
        instead, and the outcome is only logged to the check log.

        Returns (ok, out, bounced). `bounced=True` means the task was already handled (check_failed,
        requeued after exhausted retries, a stop request, or the human's protected-path gate) and
        the caller should just move on to the next task. `bounced=False` means the caller should
        apply its normal ok/out merge-result handling (merge success, or a real conflict from
        merge_checked's own `git merge`)."""
        for attempt in range(1, MERGE_RETRY_ATTEMPTS + 1):
            main_head, task_head = gitops.prepare_check(cfg.root, tree, t["branch"])
            passed, outcome = gitops.run_check(tree, cfg.git.check, cfg.git.check_timeout, log_path, CheckStop(self))
            current = s.task(t["id"])
            if self._stop.is_set() or not current or current["status"] != "approved":
                return False, outcome, True
            if not passed:
                with log_path.open(errors="replace") as log:
                    tail = "".join(deque(log, maxlen=50))[-12000:]
                self.check_failed(t, tail or outcome)
                return False, outcome, True
            files = gitops.git(tree, "diff", "--no-renames", "--name-only", main_head, task_head).splitlines()
            if needs_launch_smoke(files):
                reason = run_launch_smoke(tree, log_path)
                current = s.task(t["id"])
                if self._stop.is_set() or not current or current["status"] != "approved":
                    return False, reason or "stopped during launch smoke", True
                if reason:
                    self.check_failed(t, reason)
                    return False, reason, True
            s.kv_set(f"check_failures.{t['id']}", 0)
            if not task_gate(cfg, s, s.task(t["id"])):
                return False, "awaiting protected-path approval", True
            ok, out = gitops.merge_checked(cfg.root, tree, t["branch"], main_head, task_head,
                                           f"Merge #{t['id']}: {t['title']}", CheckStop(self),
                                           doc_only_paths=cfg.git.doc_only_paths)
            if self._stop.is_set() or self.store.kv_get("stopped"):
                return False, out, True
            if ok or not out.startswith("Repository changed"):
                return ok, out, False  # success, or a real merge conflict — let the caller handle it
            with log_path.open("a") as log:
                log.write(f"{out} (attempt {attempt}/{MERGE_RETRY_ATTEMPTS})\n")
            if attempt == MERGE_RETRY_ATTEMPTS:
                requeue_at = now() + MERGE_REQUEUE_BACKOFF_SECONDS
                with log_path.open("a") as log:
                    log.write(f"Repository changed during checks {MERGE_RETRY_ATTEMPTS} times in a row — "
                              f"real code movement, not doc/spec-only. Staying approved; requeued in "
                              f"{MERGE_REQUEUE_BACKOFF_SECONDS:g}s instead of bouncing to the builder.\n")
                s.update_task(t["id"], next_attempt_at=requeue_at)
                return False, out, True
            stop = CheckStop(self)
            if stop.wait(MERGE_RETRY_BACKOFF_SECONDS):
                return False, out, True  # stopped during backoff — leave the task as approved

    def _process_approved(self) -> None:
        s = self.store
        process_answers(self.cfg, s)
        for t in s.tasks(("approved",)):
            if now() < (t["next_attempt_at"] or 0):  # #107: sitting out a requeue backoff
                continue
            if s.kv_get("stopped") or not task_gate(self.cfg, s, t):
                continue
            if self._stop.is_set() or self.store.kv_get("stopped"):
                return
            if any(w.task and w.task["id"] == t["id"] for _, _, w in list(self.running.values())):
                continue
            if not t["branch"]:
                s.update_task(t["id"], actor="system", status="done", event_text=f"#{t['id']} done")
                continue
            cfg = self.cfg
            if cfg.git.check:
                s.kv_set("checking_task", t["id"])
                tree = Path(t["worktree"]) if t["worktree"] else None
                try:
                    if tree is None or not tree.exists():
                        self.check_failed(t, "Task worktree is missing; restore it and resubmit.")
                        continue
                    log_path = cfg.state_dir / "checks" / f"t{t['id']}.log"
                    ok, out, bounced = self._check_and_merge_with_retry(cfg, s, t, tree, log_path)
                    if bounced:
                        continue
                except gitops.GitError as e:
                    ok, out = False, str(e)
                except OSError as e:
                    self.check_failed(t, f"Could not run checks: {e}")
                    continue
                finally:
                    s.kv_set("checking_task", None)
            else:
                ok, out = gitops.merge_branch(cfg.root, t["branch"], f"Merge #{t['id']}: {t['title']}")
            if ok:
                s.kv_set(f"check_failures.{t['id']}", 0)
                try:
                    if t["worktree"]:
                        gitops.remove_worktree(self.cfg.root, Path(t["worktree"]))
                    gitops.delete_branch(self.cfg.root, t["branch"])
                except gitops.GitError as e:
                    s.event("system", "error", f"Merged #{t['id']}, but cleanup failed: {e}", significant=False)
                s.update_task(t["id"], actor="system", status="done", worktree=None,
                              event_text=f"Merged #{t['id']} {t['title']} into main")
                if t["assignee"]:
                    s.send("system", t["assignee"], f"Your task #{t['id']} passed review and was merged into main.",
                           subject=f"#{t['id']} merged", task_id=t["id"], kind="system")
                    s.mark_read([m["id"] for m in s.unread(t["assignee"]) if m["kind"] == "system"])
            else:
                s.update_task(t["id"], actor="system", status="in_progress", next_attempt_at=0,
                              review_notes=f"Merge conflict with main:\n{out[-1500:]}",
                              event_text=f"Merge conflict on #{t['id']} — back to {t['assignee']}")
                if t["assignee"]:
                    base = gitops.current_branch(self.cfg.root)
                    s.send("system", t["assignee"],
                           f"QA approved #{t['id']} but it conflicts with {base}. In your worktree run "
                           f"`git merge {base}`, resolve the conflicts, re-run the tests, commit, then "
                           f"complete_task again.\n\n{out[-1200:]}",
                           subject=f"#{t['id']} merge conflict", task_id=t["id"], kind="system")

