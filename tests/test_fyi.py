"""#74/REQ-COM-013: FYI mail is a deterministic rule — never a wake, never a model call.

Human: "if it's FYI, don't invoke; that's a script, not an LLM wakeup." Follow-up to #46
(REQ-ENG-049's local-LLM triage): mail marked fyi=True, or with a subject starting "FYI", is held
for the recipient's next real wake and never reaches triage or any other model. Triage stays for
mail that's neither FYI nor obviously actionable.
"""
import asyncio

import pytest

from troupe.api import APIServer, Data
from troupe.engine import Engine, Wake
from troupe.store import now
from troupe.team import TeamAPI
from troupe.triage import TriageSettings


def setup(project, monkeypatch):
    cfg, store = project
    engine = Engine(cfg)
    monkeypatch.setattr('troupe.engine.now', lambda: 1000)
    return cfg, store, engine


def mail(store, subject='', **kw):
    mid = store.send('pm', 'lead', 'Deployment completed successfully', subject=subject, **kw)
    store.x('UPDATE messages SET ts=990 WHERE id=?', mid)
    return mid


def reasons(engine):
    return [w.reason for w in engine.candidates(False) if w.agent.id == 'lead']


def engine_state(cfg):
    return Data(APIServer(cfg)).engine_state()


# ── zero runs, zero model calls for FYI mail ────────────────────────────────

def test_fyi_flag_causes_zero_runs_and_zero_triage_calls(project, monkeypatch):
    cfg, store, engine = setup(project, monkeypatch)
    cfg.triage = TriageSettings(enabled=True)
    async def forbidden(*args):
        pytest.fail('FYI mail must never reach the triage model')
    monkeypatch.setattr('troupe.triage.classify', forbidden)
    launched = []
    async def launch(w):
        launched.append(w)
    monkeypatch.setattr(engine, 'launch', launch)
    monkeypatch.setattr(engine, 'process_approved', lambda: None)
    mail(store, fyi=True)
    async def scenario():
        await asyncio.wait_for(engine.tick(), 2)
    asyncio.run(scenario())
    assert not any(w.agent.id == 'lead' for w in launched)
    assert not engine.mail_triage.entries


def test_fyi_subject_prefix_without_the_flag_is_also_deterministic(project, monkeypatch):
    """The team convention predates the fyi=True flag: a subject starting "FYI" gets the same
    treatment even if the caller forgot the flag (REQ-COM-013)."""
    cfg, store, engine = setup(project, monkeypatch)
    cfg.triage = TriageSettings(enabled=True)
    async def forbidden(*args):
        pytest.fail('an "FYI"-subject message must never reach the triage model either')
    monkeypatch.setattr('troupe.triage.classify', forbidden)
    mail(store, subject='FYI: build is green')
    assert reasons(engine) == []
    assert not engine.mail_triage.entries


@pytest.mark.parametrize('subject', ['fyi', 'FYI:', 'Fyi - heads up', '  FYI  nightly build'])
def test_fyi_subject_prefix_is_case_and_punctuation_insensitive(project, monkeypatch, subject):
    cfg, store, engine = setup(project, monkeypatch)
    mail(store, subject=subject)
    assert reasons(engine) == []


def test_non_fyi_subject_mentioning_fyi_later_is_not_treated_as_fyi(project, monkeypatch):
    cfg, store, engine = setup(project, monkeypatch)
    mail(store, subject='Heads up, FYI this matters')
    assert reasons(engine) == ['messages']


# ── FYI content still reaches the recipient later ───────────────────────────

def test_fyi_reaches_recipient_in_next_real_wake_digest(project, monkeypatch):
    cfg, store, engine = setup(project, monkeypatch)
    mail(store, fyi=True)
    assert reasons(engine) == []
    messages = store.unread('lead')
    engine.pokes.add('lead')
    assert reasons(engine) == ['poke']
    prompt = engine.build_prompt(cfg.agent('lead'), Wake(1, cfg.agent('lead'), 'poke'), messages, None, {})
    assert 'FYI since last time' in prompt and 'Deployment completed successfully' in prompt
    assert store.unread('lead') == messages  # a hold never marks mail read


def test_fyi_subject_prefix_also_groups_into_the_fyi_digest(project, monkeypatch):
    cfg, store, engine = setup(project, monkeypatch)
    mid = mail(store, subject='FYI: nightly build')
    messages = store.unread('lead')
    prompt = engine.build_prompt(cfg.agent('lead'), Wake(1, cfg.agent('lead'), 'poke'), messages, None, {})
    assert 'FYI since last time' in prompt
    assert f'[msg #{mid}]' in prompt.split('FYI since last time')[1]


# ── ambiguous mail still uses triage; hard rules still bypass it ───────────

def test_ambiguous_non_fyi_mail_still_uses_triage(project, monkeypatch):
    cfg, store, engine = setup(project, monkeypatch)
    cfg.triage = TriageSettings(enabled=True)
    calls = []
    async def hold(*args):
        calls.append(args)
        return {'wake_now': False, 'reason': 'info', 'digest': 'status'}
    monkeypatch.setattr('troupe.triage.classify', hold)
    mail(store)  # plain status update: neither fyi nor obviously actionable
    async def scenario():
        assert reasons(engine) == []
        await _settle(engine)
    asyncio.run(scenario())
    assert len(calls) == 1


@pytest.mark.parametrize('kind', ['question', 'review', 'own_task'])
def test_hard_rules_still_bypass_triage_with_fyi_set(project, monkeypatch, kind):
    """fyi=True never overrides a hard rule (human mail, questions, reviews, own-task mail):
    mandatory() runs first in fyi_message(), same guarantee as before #74."""
    cfg, store, engine = setup(project, monkeypatch)
    cfg.triage = TriageSettings(enabled=True)
    async def forbidden(*args):
        pytest.fail('hard rules must not invoke a model')
    monkeypatch.setattr('troupe.triage.classify', forbidden)
    if kind == 'question':
        store.send('pm', 'lead', 'Which option?', fyi=True)
    elif kind == 'review':
        store.send('pm', 'lead', 'Review requested', fyi=True)
    else:
        tid = store.add_task('Owned', assignee='lead', status='blocked')
        store.send('pm', 'lead', f'Update for #{tid}', task_id=tid, fyi=True)
    store.x('UPDATE messages SET ts=990')
    assert reasons(engine) == ['messages']
    assert not engine.mail_triage.entries


async def _settle(engine):
    if engine.mail_triage.entries:
        await asyncio.gather(*(t for _, t in engine.mail_triage.entries.values()))


# ── API counters ─────────────────────────────────────────────────────────

def test_fyi_counters_increase_for_avoided_wakes_and_model_calls(project, monkeypatch):
    cfg, store, engine = setup(project, monkeypatch)
    cfg.triage = TriageSettings(enabled=True)
    before = engine_state(cfg)
    assert before['fyi_wakes_avoided_1h'] == 0 and before['fyi_model_calls_avoided_1h'] == 0
    mail(store, fyi=True)
    assert reasons(engine) == []
    after = engine_state(cfg)
    assert after['fyi_wakes_avoided_1h'] == 1
    assert after['fyi_model_calls_avoided_1h'] == 1  # triage was enabled, so a call was avoided too


def test_model_calls_avoided_stays_zero_when_triage_is_disabled(project, monkeypatch):
    """No triage is configured, so there was never a model call to avoid — only the wake counter
    should move."""
    cfg, store, engine = setup(project, monkeypatch)
    assert not cfg.triage.enabled
    mail(store, fyi=True)
    assert reasons(engine) == []
    state = engine_state(cfg)
    assert state['fyi_wakes_avoided_1h'] == 1
    assert state['fyi_model_calls_avoided_1h'] == 0


def test_counters_do_not_double_count_the_same_batch_across_ticks(project, monkeypatch):
    """should_wake() runs every engine tick while mail sits unread; the same held batch must be
    credited once, not once per tick."""
    cfg, store, engine = setup(project, monkeypatch)
    cfg.triage = TriageSettings(enabled=True)
    mail(store, fyi=True)
    for _ in range(5):
        assert reasons(engine) == []
    state = engine_state(cfg)
    assert state['fyi_wakes_avoided_1h'] == 1 and state['fyi_model_calls_avoided_1h'] == 1


def test_counters_increment_again_for_a_later_distinct_fyi_batch(project, monkeypatch):
    cfg, store, engine = setup(project, monkeypatch)
    mail(store, fyi=True)
    assert reasons(engine) == []
    mail(store, fyi=True)  # a second, later FYI arrival — a genuinely new batch
    assert reasons(engine) == []
    state = engine_state(cfg)
    assert state['fyi_wakes_avoided_1h'] == 2


def test_counters_only_cover_the_last_hour(project, monkeypatch):
    cfg, store, engine = setup(project, monkeypatch)
    mail(store, fyi=True)
    assert reasons(engine) == []
    assert engine_state(cfg)['fyi_wakes_avoided_1h'] == 1
    store.x("UPDATE events SET ts=? WHERE kind='fyi_wake_avoided'", now() - 3700)
    assert engine_state(cfg)['fyi_wakes_avoided_1h'] == 0


def test_fyi_metric_events_are_not_significant(project, monkeypatch):
    """Must not leak into any agent's "what happened since you last looked" digest (that filters on
    significant=1) — these are bookkeeping, not team activity."""
    cfg, store, engine = setup(project, monkeypatch)
    mail(store, fyi=True)
    assert reasons(engine) == []
    events = [e for e in store.events() if e['kind'] in ('fyi_wake_avoided', 'fyi_model_call_avoided')]
    assert events and all(e['significant'] == 0 for e in events)
