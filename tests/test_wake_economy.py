"""#121: runs that produce nothing cost real money. Evidence from today's engine.log: agents woken
only to say "duplicate delivery of the same QA rejection I already addressed", "stale replay of the
same rejection", or "recorded, no reply needed" for a broadcast. This suite covers the engine-side
guarantees: a message a previous run already saw (and whose work is now done) never wakes a second
time; fyi mail never wakes by itself; a fyi=True team broadcast isn't re-flagged mandatory just for
using ordinary words like "please"/"review"; idle_minutes overrides are honoured; a burst of arrivals
coalesces into one run; and a seeded simulation shows wasted runs stay low.
"""
import asyncio

import pytest

from troupe.engine import Engine, MESSAGE_DEBOUNCE, Wake
from troupe.runners import RunResult
from troupe.triage import fyi_message, mandatory


class FakeRunner:
    cancelled = False

    def __init__(self, result):
        self.result = result

    async def run(self, spec, emit):
        emit("text", "working")
        return self.result


async def run_wake(engine, wake):
    await engine.launch(wake)
    await engine.running[wake.agent.id][1]


def reasons(engine, agent_id):
    return [w.reason for w in engine.candidates(False) if w.agent.id == agent_id]


# ── item 1: a message a previous run already handled never wakes again ──────
#
# QA #121 round 1 (rejected): a task's *current status* proves nothing about whether a given
# recipient read a given message — a reviewer is mailed about tasks in "review", a builder about
# "ready" ones, a lead about any status. The fix is evidence-based instead: mark a message read
# only when *that message's own recipient* has, since the message arrived, authored a task event
# for its task_id (update_task/complete_task/review_task all log one). Everything else redelivers,
# exactly like the pre-#121 behavior.

def test_orphaned_run_whose_recipient_already_acted_on_the_task_does_not_redeliver_its_mail(project, monkeypatch):
    """#121 AC1: a message already delivered in a previous run must never cause another wake once
    its own recipient has, in fact, acted on that task since. Reproduces today's actual driver: the
    engine (or process) dies between the builder's own complete_task call landing and that run's
    end_run() finalizing its row, so the run is still 'running' in the DB even though the work it
    was mailed about is already finished."""
    cfg, store = project
    monkeypatch.setattr('troupe.engine.now', lambda: 1000)
    engine = Engine(cfg)
    tid = store.add_task('Fix the thing', assignee='builder-1', status='in_progress')
    mid = store.send('qa', 'builder-1', 'needs changes', subject=f'Review: #{tid}', task_id=tid)
    store.x('UPDATE messages SET ts=990')
    store.mark_read([mid])  # this run already saw it
    rid = store.start_run('builder-1', 'messages', tid, str(cfg.root), False)
    store.kv_set(f'run_mail.{rid}', [mid])
    # the run's own complete_task call landed (actor=builder-1, an event on task:tid after ts=990)
    store.update_task(tid, actor='builder-1', event_text='builder-1 completed it', status='review')
    # run row is still 'running': the process died before end_run() got a chance to run

    engine.sweep_zombie_runs()

    assert store.unread('builder-1') == []  # not redelivered
    assert reasons(engine, 'builder-1') == []  # no fresh wake for it


def test_orphaned_run_whose_recipient_never_touched_the_task_still_redelivers_its_mail(project, monkeypatch):
    """The safety net: if the recipient never actually acted on the task, the mail must still come
    back — #121's fix must never risk silently dropping real unaddressed work."""
    cfg, store = project
    monkeypatch.setattr('troupe.engine.now', lambda: 1000)
    engine = Engine(cfg)
    tid = store.add_task('Fix the thing', assignee='builder-1', status='in_progress')
    mid = store.send('qa', 'builder-1', 'needs changes', subject=f'Review: #{tid}', task_id=tid)
    store.x('UPDATE messages SET ts=990')
    store.mark_read([mid])
    rid = store.start_run('builder-1', 'messages', tid, str(cfg.root), False)
    store.kv_set(f'run_mail.{rid}', [mid])
    # task never touched by builder-1 — the run genuinely never got to it

    engine.sweep_zombie_runs()

    assert len(store.unread('builder-1')) == 1
    assert reasons(engine, 'builder-1') == ['messages']


def test_a_tasks_status_moving_for_some_other_reason_does_not_by_itself_clear_the_mail(project, monkeypatch):
    """QA's exact regression repro shape: the task's status changes (by dispatch, by someone else,
    or just because that's the status mail about this role normally arrives in) without the
    *recipient* having authored anything — must still redeliver. Status alone is never evidence."""
    cfg, store = project
    monkeypatch.setattr('troupe.engine.now', lambda: 1000)
    engine = Engine(cfg)
    tid = store.add_task('Fix the thing', assignee='builder-1', status='in_progress')
    mid = store.send('qa', 'builder-1', 'needs changes', subject=f'Review: #{tid}', task_id=tid)
    store.x('UPDATE messages SET ts=990')
    store.mark_read([mid])
    rid = store.start_run('builder-1', 'messages', tid, str(cfg.root), False)
    store.kv_set(f'run_mail.{rid}', [mid])
    store.update_task(tid, actor='system', event_text='moved by someone else', status='review')

    engine.sweep_zombie_runs()

    assert len(store.unread('builder-1')) == 1
    assert reasons(engine, 'builder-1') == ['messages']


def test_orphaned_run_only_clears_the_mail_tied_to_the_finished_task_not_unrelated_mail(project, monkeypatch):
    """The per-message check must key off *each message's own* task_id, not the run's task as a
    whole — unrelated mail riding along in the same batch must still come back."""
    cfg, store = project
    monkeypatch.setattr('troupe.engine.now', lambda: 1000)
    engine = Engine(cfg)
    tid = store.add_task('Fix the thing', assignee='builder-1', status='in_progress')
    done_mid = store.send('qa', 'builder-1', 'needs changes', subject=f'Review: #{tid}', task_id=tid)
    other_mid = store.send('lead', 'builder-1', 'unrelated context, not about this task')
    store.x('UPDATE messages SET ts=990')
    store.mark_read([done_mid, other_mid])
    rid = store.start_run('builder-1', 'messages', tid, str(cfg.root), False)
    store.kv_set(f'run_mail.{rid}', [done_mid, other_mid])
    store.update_task(tid, actor='builder-1', event_text='builder-1 completed it', status='review')

    engine.sweep_zombie_runs()

    unread_ids = {m['id'] for m in store.unread('builder-1')}
    assert unread_ids == {other_mid}  # only the finished-task mail was cleared


def test_engine_startup_recovery_also_skips_mail_for_finished_work(project, monkeypatch):
    """The same guarantee at process-restart time (Engine.recover(), not just the per-tick sweep) —
    a reinstall/restart racing a run whose complete_task call already landed."""
    cfg, store = project
    monkeypatch.setattr('troupe.engine.now', lambda: 1000)
    engine = Engine(cfg)
    tid = store.add_task('Fix the thing', assignee='builder-1', status='in_progress')
    mid = store.send('qa', 'builder-1', 'needs changes', subject=f'Review: #{tid}', task_id=tid)
    store.x('UPDATE messages SET ts=990')
    store.mark_read([mid])
    rid = store.start_run('builder-1', 'messages', tid, str(cfg.root), False)
    store.kv_set(f'run_mail.{rid}', [mid])
    store.update_task(tid, actor='builder-1', event_text='builder-1 completed it', status='review')
    store.set_agent('builder-1', state='running', current_run=rid)

    engine._recover_interrupted_runs()

    assert store.unread('builder-1') == []
    assert store.one('SELECT status FROM runs WHERE id=?', rid)['status'] == 'interrupted'
    assert store.agent('builder-1')['state'] == 'idle'


def test_watchdog_kill_does_not_redeliver_mail_for_a_task_the_recipient_finished_first(project, monkeypatch):
    """The same live, same-process path (REQ-ENG-050): the backend already called complete_task,
    then the session hung on something unrelated afterward and got stall-killed. The original
    mail must not be redelivered — that's the literal "I already addressed this" run."""
    cfg, store = project
    monkeypatch.setattr('troupe.engine.now', lambda: 1000)
    engine = Engine(cfg)
    monkeypatch.setattr(engine, 'process_approved', lambda: None)
    tid = store.add_task('Fix the thing', assignee='builder-1', status='in_progress')
    mid = store.send('qa', 'builder-1', 'needs changes', subject=f'Review: #{tid}', task_id=tid)
    store.x('UPDATE messages SET ts=990')

    class HangingRunner(FakeRunner):
        async def run(self, spec, emit):
            # the tool call lands mid-run, authored by the recipient itself
            store.update_task(tid, actor='builder-1', event_text='builder-1 completed it', status='review')
            engine._watchdog_reason['builder-1'] = 'stalled'  # then the watchdog kills it
            return RunResult(ok=False, error='killed')

    monkeypatch.setattr('troupe.engine.make_runner', lambda backend: HangingRunner(None))
    asyncio.run(run_wake(engine, Wake(2, cfg.agent('builder-1'), 'messages', store.task(tid))))

    assert store.unread('builder-1') == []
    assert reasons(engine, 'builder-1') == []


def test_rate_limited_run_that_did_no_work_still_redelivers_mail_about_a_review_task(project, monkeypatch):
    """QA #121 round 1 repro (a): a task in "review" (the status a reviewer is normally mailed
    about); the run comes back rate-limited having done nothing. Must redeliver — a task's own
    status ("review") is exactly the kind of thing that used to look like false evidence."""
    cfg, store = project
    monkeypatch.setattr('troupe.engine.now', lambda: 1000)
    engine = Engine(cfg)
    monkeypatch.setattr(engine, 'process_approved', lambda: None)
    tid = store.add_task('Needs review', assignee='builder-1', status='review')
    mid = store.send('lead', 'qa', f'Review #{tid} first; checklist: run the tests, check the diff.',
                     subject=f'Review: #{tid}', task_id=tid)
    store.x('UPDATE messages SET ts=990')

    runner = FakeRunner(RunResult(ok=False, error='rate limited', extra={'limit_until': 2000}))
    monkeypatch.setattr('troupe.engine.make_runner', lambda backend: runner)
    asyncio.run(run_wake(engine, Wake(2, cfg.agent('qa'), 'messages', store.task(tid))))

    assert len(store.unread('qa')) == 1  # not dropped -- still there once the limit clears
    monkeypatch.setattr('troupe.engine.now', lambda: 2001.0)  # past limit_until: qa is wakeable again
    # 'review', not 'messages': the task's own review-queue candidacy (a separate, pre-existing
    # mechanism) outranks it -- either way the redelivered mail rides along in that same wake.
    assert reasons(engine, 'qa') == ['review']


def test_failed_run_that_did_no_work_still_redelivers_mail_about_a_ready_task(project, monkeypatch):
    """QA #121 round 1 repro (b): a task in "ready" (the status a builder is normally mailed about);
    the run fails outright. Must redeliver."""
    cfg, store = project
    monkeypatch.setattr('troupe.engine.now', lambda: 1000)
    engine = Engine(cfg)
    monkeypatch.setattr(engine, 'process_approved', lambda: None)
    tid = store.add_task('Pick this up', status='ready')
    mid = store.send('lead', 'builder-1', f'Heads up on #{tid} before you start.', task_id=tid)
    store.x('UPDATE messages SET ts=990')

    runner = FakeRunner(RunResult(ok=False, error='backend crashed'))
    monkeypatch.setattr('troupe.engine.make_runner', lambda backend: runner)
    asyncio.run(run_wake(engine, Wake(2, cfg.agent('builder-1'), 'messages', store.task(tid))))

    assert len(store.unread('builder-1')) == 1  # not dropped
    monkeypatch.setattr('troupe.engine.now', lambda: 1100.0)  # past the failure backoff
    assert reasons(engine, 'builder-1') == ['messages']
    assert reasons(engine, 'builder-1') == ['messages']


# ── item 2: fyi=True alone never wakes; it's folded into the next real wake ─

def test_fyi_message_alone_causes_no_run_and_appears_in_the_next_wake(project, monkeypatch):
    cfg, store = project
    monkeypatch.setattr('troupe.engine.now', lambda: 1000)
    engine = Engine(cfg)
    store.send('pm', 'lead', 'Build finished green', fyi=True)
    store.x('UPDATE messages SET ts=990')

    assert reasons(engine, 'lead') == []  # no run by itself

    engine.pokes.add('lead')  # the next real wake, for any other reason
    assert reasons(engine, 'lead') == ['poke']
    msgs = store.unread('lead')
    prompt = engine.build_prompt(cfg.agent('lead'), Wake(1, cfg.agent('lead'), 'poke'), msgs, None, {})
    assert 'FYI since last time' in prompt and 'Build finished green' in prompt


# ── item 3: a team broadcast doesn't wake someone with nothing else to do ───

def test_fyi_broadcast_with_ordinary_prose_does_not_wake_any_recipient(project, monkeypatch):
    """#121 AC3: a team broadcast the sender marked fyi=True (the interim policy) must not wake a
    recipient who has nothing else to do, even though its prose contains words ("please", "review")
    that would make a *direct* message mandatory."""
    cfg, store = project
    monkeypatch.setattr('troupe.engine.now', lambda: 1000)
    engine = Engine(cfg)
    body = 'Please hold non-urgent edits; a review is in progress on the merge gate.'
    for recipient in ('lead', 'pm', 'spec', 'designer', 'qa'):
        store.send('gadfly', recipient, body, subject='Heads up', fyi=True)
    store.x('UPDATE messages SET ts=990')

    for recipient in ('lead', 'pm', 'spec', 'designer', 'qa'):
        assert reasons(engine, recipient) == [], f'{recipient} should not wake for the broadcast'


def test_direct_fyi_message_with_the_same_prose_still_wakes_as_a_hard_rule(project, monkeypatch):
    """The narrow, deliberate boundary: a *single-recipient* fyi=True message with the same kind of
    language keeps the existing keyword hard rule (test_hard_rules_bypass_fyi_and_model) — only a
    corroborated broadcast (siblings to other recipients) gets the carve-out."""
    cfg, store = project
    monkeypatch.setattr('troupe.engine.now', lambda: 1000)
    engine = Engine(cfg)
    store.send('gadfly', 'lead', 'Please review this when you have a moment.', fyi=True)
    store.x('UPDATE messages SET ts=990')

    assert reasons(engine, 'lead') == ['messages']


def test_broadcast_without_explicit_fyi_is_unaffected_by_broadcast_detection(project, monkeypatch):
    """Broadcast detection only ever corroborates an explicit fyi=True; it must never invent fyi
    status for a multi-recipient send the sender didn't flag (content/timing alone is not a
    reliable enough signal — see test_dispatch_capacity.py's identical-text coordination mail)."""
    cfg, store = project
    monkeypatch.setattr('troupe.engine.now', lambda: 1000)
    engine = Engine(cfg)
    for recipient in ('lead', 'pm', 'spec'):
        store.send('qa', recipient, 'Please review the merge queue.')  # no fyi=True
    store.x('UPDATE messages SET ts=990')

    for recipient in ('lead', 'pm', 'spec'):
        assert reasons(engine, recipient) == ['messages']


def test_broadcast_containing_a_real_question_still_wakes(project, monkeypatch):
    """#121 AC3's own carve-out: "unless they contain a question" — a broadcast still wakes if it
    actually asks one, even with fyi=True and siblings."""
    cfg, store = project
    monkeypatch.setattr('troupe.engine.now', lambda: 1000)
    engine = Engine(cfg)
    for recipient in ('lead', 'pm'):
        store.send('gadfly', recipient, 'Does anyone object to this plan?', fyi=True)
    store.x('UPDATE messages SET ts=990')

    for recipient in ('lead', 'pm'):
        assert reasons(engine, recipient) == ['messages']


# ── item 4: idle_minutes overrides are honoured (engine-side only; roles.py is protected) ──

def test_idle_minutes_zero_never_triggers_a_proactive_wake(project, monkeypatch):
    cfg, store = project
    monkeypatch.setattr('troupe.engine.now', lambda: 100000)
    cfg.agent('builder-1').idle_minutes = 0
    engine = Engine(cfg)
    store.set_agent('builder-1', last_run_at=0, last_event_seen=0)
    store.event('lead', 'note', 'something changed', significant=True)

    assert reasons(engine, 'builder-1') == []


def test_idle_minutes_override_fires_once_the_interval_elapses(project, monkeypatch):
    cfg, store = project
    engine = Engine(cfg)
    cfg.agent('lead').idle_minutes = 10
    store.set_agent('lead', last_run_at=0, last_event_seen=0)
    store.event('pm', 'note', 'something changed', significant=True)

    monkeypatch.setattr('troupe.engine.now', lambda: 9 * 60)
    assert reasons(engine, 'lead') == []  # not yet

    monkeypatch.setattr('troupe.engine.now', lambda: 11 * 60)
    assert reasons(engine, 'lead') == ['proactive']


# ── item 5: a burst of arrivals within MESSAGE_DEBOUNCE coalesces into one run ──

def test_message_burst_within_debounce_window_delivers_as_one_batch(project, monkeypatch):
    cfg, store = project
    t = [1000.0]
    monkeypatch.setattr('troupe.engine.now', lambda: t[0])
    engine = Engine(cfg)
    store.send('pm', 'lead', 'First update')
    store.x('UPDATE messages SET ts=?', t[0])
    t[0] += MESSAGE_DEBOUNCE / 2
    store.send('pm', 'lead', 'Second update')
    store.x("UPDATE messages SET ts=? WHERE body='Second update'", t[0])

    assert reasons(engine, 'lead') == []  # still inside the debounce window

    t[0] += MESSAGE_DEBOUNCE
    assert reasons(engine, 'lead') == ['messages']
    msgs = store.unread('lead')
    assert len(msgs) == 2  # both arrivals land in the same wake, not two separate runs


# ── seeded simulation: wasted runs stay low ──────────────────────────────────

def _run_seeded_hour(cfg, store, engine, monkeypatch) -> int:
    """Replays a seeded hour (ticks advance a monkeypatched clock; no real sleeping) of today's
    evidence patterns — a QA-rejection mail redelivered after the fix already landed, and fyi=True
    team broadcasts with ordinary "please"/"review" prose — and returns the count of runs that made
    no tool call and sent no message. `engine.now`, `make_runner` and `process_approved` must already
    be patched by the caller; this only drives the loop."""
    t = [0.0]
    monkeypatch.setattr('troupe.engine.now', lambda: t[0])
    for a in cfg.agents:
        a.idle_minutes = 0  # proactive check-ins are item 4's own concern, tested separately above;
        # isolate this simulation to items 1 and 3's seeded triggers.

    wasted = 0
    for minute in range(60):
        t[0] = minute * 60.0
        if minute % 5 == 0:
            # A QA rejection whose fix already landed before this "restart" (today's driver).
            tid = store.add_task(f'Task {minute}', assignee='builder-1', status='in_progress')
            mid = store.send('qa', 'builder-1', 'needs changes', subject=f'Review: #{tid}', task_id=tid)
            store.mark_read([mid])
            rid = store.start_run('builder-1', 'messages', tid, str(cfg.root), False)
            store.kv_set(f'run_mail.{rid}', [mid])
            # done, not review: a task left sitting in review would legitimately keep waking QA
            # every tick until reviewed -- that's the review queue working as intended, not a
            # wake-economy bug, and this simulation's fake backend never resolves it. actor/
            # event_text=builder-1 is the evidence _requeue_or_deliver_read looks for.
            store.update_task(tid, actor='builder-1', event_text='builder-1 completed it', status='done')
        if minute % 10 == 0:
            # A fyi=True team broadcast with ordinary "please"/"review" prose.
            for recipient in ('lead', 'pm', 'spec', 'designer', 'qa'):
                store.send('gadfly', recipient, 'Please note the new review policy.', fyi=True)
        store.x('UPDATE messages SET ts=?', t[0] - MESSAGE_DEBOUNCE - 1)  # clear the debounce window
        engine.sweep_zombie_runs()
        for w in sorted(engine.candidates(False), key=lambda w: w.priority):
            if w.agent.id in engine.running:
                continue
            asyncio.run(run_wake(engine, w))
            run = store.runs(limit=1)[0]
            if run['status'] == 'ok' and not store.q("SELECT 1 FROM run_lines WHERE run_id=? AND kind='tool'", run['id']):
                wasted += 1
    return wasted


def test_seeded_hour_simulation_wasted_runs_drop_at_least_80pct_vs_pre_121(project, monkeypatch):
    """#121 acceptance: over a seeded simulation with fake backends, runs that make no tool call and
    send no message drop by at least 80% versus the pre-#121 behavior — same seed, same fake
    backend, only the #121 guards (the broadcast carve-out in mandatory(), and task-aware
    redelivery instead of blind mark_unread) toggled off for the "before" run."""
    import re as _re
    from troupe.store import OPEN_STATUSES

    def pre_121_mandatory(message, agent, s):
        """The exact pre-#121 mandatory(): same hard rules, minus the broadcast carve-out."""
        if message['sender'] == 'human':
            return True
        text = message['subject'] + '\n' + message['body']
        if '?' in text or _re.search(r'\b(question|blocking|blocked|review|please|action required)\b', text, _re.I):
            return True
        ids = [message['task_id']] if message.get('task_id') else [int(x) for x in _re.findall(r'#(\d+)\b', text)]
        tasks = [s.task(tid) for tid in ids]
        return any(task and task['status'] in OPEN_STATUSES and (
            task['assignee'] == agent.id or task['reviewer'] == agent.id
            or (agent.role == 'qa' and task['status'] == 'review' and not task['reviewer'])) for task in tasks)

    cfg, store = project

    class NoOpRunner(FakeRunner):
        def __init__(self):
            super().__init__(RunResult(ok=True, final_text=''))

    monkeypatch.setattr('troupe.engine.make_runner', lambda backend: NoOpRunner())

    before_engine = Engine(cfg)
    monkeypatch.setattr(before_engine, 'process_approved', lambda: None)
    monkeypatch.setattr('troupe.engine.mandatory', pre_121_mandatory)
    monkeypatch.setattr('troupe.triage.mandatory', pre_121_mandatory)
    monkeypatch.setattr(before_engine, '_requeue_or_deliver_read',
                        lambda ids: store.mark_unread(ids))  # the old, blind behavior
    before = _run_seeded_hour(cfg, store, before_engine, monkeypatch)

    store.x('DELETE FROM messages')
    store.x('DELETE FROM tasks')
    store.x('DELETE FROM runs')
    store.x('DELETE FROM kv')
    monkeypatch.undo()  # restore the real mandatory() and now() before re-patching for the "after" run
    monkeypatch.setattr('troupe.engine.make_runner', lambda backend: NoOpRunner())
    after_engine = Engine(cfg)
    monkeypatch.setattr(after_engine, 'process_approved', lambda: None)
    after = _run_seeded_hour(cfg, store, after_engine, monkeypatch)

    assert before >= 15  # the seeded patterns are a real cost driver without the fixes
    # `after` isn't literally zero: cfg.triage.max_pending (5) is an intentional, pre-existing
    # safety valve that periodically flushes an accumulated fyi backlog rather than holding it
    # forever — the one legitimate flush the seeded broadcasts trigger over the hour.
    assert after <= 6
    assert after <= before * 0.2  # at least an 80% drop
