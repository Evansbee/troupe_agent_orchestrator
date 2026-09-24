"""FYI delivery, model fail-open behavior and synthetic mail-load replay."""
import asyncio
import json
import sqlite3

import pytest

from troupe import config
from troupe.engine import Engine, Wake
from troupe.store import Store
from troupe.team import TeamAPI
from troupe.triage import TriageSettings, classify


def setup(project, monkeypatch):
    cfg, store = project
    engine = Engine(cfg)
    monkeypatch.setattr('troupe.engine.now', lambda: 1000)
    return cfg, store, engine


def mail(store, **kw):
    mid = store.send('pm', 'lead', 'Deployment completed successfully', **kw)
    store.x('UPDATE messages SET ts=990 WHERE id=?', mid)
    return mid


def reasons(engine):
    return [w.reason for w in engine.candidates(False) if w.agent.id == 'lead']


async def settled(engine):
    if engine.mail_triage.entries:
        await asyncio.gather(*(t for _, t in engine.mail_triage.entries.values()))


def test_fyi_stays_unread_until_next_natural_wake(project, monkeypatch):
    cfg, store, engine = setup(project, monkeypatch)
    TeamAPI(cfg, store, 'pm').send_message('lead', 'All done', fyi=True)
    store.x('UPDATE messages SET ts=990')
    assert reasons(engine) == []
    messages = store.unread('lead')
    assert messages[0]['fyi'] == 1
    engine.pokes.add('lead')
    assert reasons(engine) == ['poke']
    prompt = engine.build_prompt(cfg.agent('lead'), Wake(1, cfg.agent('lead'), 'poke'), messages, None, {})
    assert 'FYI since last time' in prompt and 'All done' in prompt
    assert store.unread('lead') == messages


@pytest.mark.parametrize('kind', ['human', 'question', 'review', 'own_task', 'task_in_text', 'count'])
def test_hard_rules_bypass_fyi_and_model(project, monkeypatch, kind):
    cfg, store, engine = setup(project, monkeypatch)
    cfg.triage = TriageSettings(enabled=True)
    async def forbidden(*args):
        pytest.fail('hard rules must not invoke a model')
    monkeypatch.setattr('troupe.triage.classify', forbidden)
    if kind == 'human':
        store.send('human', 'lead', 'Hello', fyi=True)
    elif kind == 'question':
        store.send('pm', 'lead', 'Which option?', fyi=True)
    elif kind == 'review':
        store.send('pm', 'lead', 'Review requested', fyi=True)
    elif kind in ('own_task', 'task_in_text'):
        tid = store.add_task('Owned', assignee='lead', status='blocked')
        store.send('pm', 'lead', f'Update for #{tid}', task_id=tid if kind == 'own_task' else None, fyi=True)
    else:
        for _ in range(cfg.triage.max_pending):
            mail(store, fyi=True)
    store.x('UPDATE messages SET ts=990')
    assert reasons(engine) == (['chat'] if kind == 'human' else ['messages'])
    assert not engine.mail_triage.entries


def test_hold_is_cached_and_delivered_on_later_poke(project, monkeypatch):
    cfg, store, engine = setup(project, monkeypatch)
    cfg.triage = TriageSettings(enabled=True, model='small')
    calls = []
    async def hold(*args):
        calls.append(args[2])
        return {'wake_now': False, 'reason': 'No action', 'digest': 'Two status updates'}
    monkeypatch.setattr('troupe.triage.classify', hold)
    mail(store)
    mail(store)
    async def scenario():
        assert reasons(engine) == []
        await settled(engine)
        for _ in range(5):
            assert reasons(engine) == []  # pending mail cannot trigger cadence indirectly
        assert len(calls) == 1 and len(calls[0]) == 2
        assert len(store.unread('lead')) == 2
        engine.pokes.add('lead')
        assert reasons(engine) == ['poke']
        prompt = engine.build_prompt(cfg.agent('lead'), Wake(1, cfg.agent('lead'), 'poke'), store.unread('lead'), None, {})
        assert '## New messages (2)' in prompt and prompt.count('[msg #') == 2
    asyncio.run(scenario())
    assert any('held 2 for lead_1@test-project' in e['text'] for e in store.events())


@pytest.mark.parametrize('failure', ['offline', 'malformed', 'timeout'])
def test_failure_wakes_normally(project, monkeypatch, failure):
    cfg, store, engine = setup(project, monkeypatch)
    cfg.triage = TriageSettings(enabled=True, timeout=.02)
    async def bad(*args):
        if failure == 'timeout':
            await asyncio.sleep(10)
        elif failure == 'offline':
            raise ConnectionError('offline')
        else:
            raise ValueError('invalid JSON')
    monkeypatch.setattr('troupe.triage.classify', bad)
    mail(store)
    async def scenario():
        assert reasons(engine) == []
        await settled(engine)
        assert reasons(engine) == ['messages']
    asyncio.run(scenario())


def test_slow_triage_does_not_block_chat_or_heartbeat(project, monkeypatch):
    cfg, store, engine = setup(project, monkeypatch)
    cfg.triage = TriageSettings(enabled=True)
    async def slow(*args):
        await asyncio.sleep(10)
    monkeypatch.setattr('troupe.triage.classify', slow)
    monkeypatch.setattr(engine, 'process_approved', lambda: None)
    launched = []
    async def launch(w):
        launched.append(w)
    monkeypatch.setattr(engine, 'launch', launch)
    mail(store)
    store.send('human', 'pm', 'Hi')
    store.set_agent('lead', last_event_seen=store.max_event_id())
    store.x('UPDATE messages SET ts=990')
    async def scenario():
        await asyncio.wait_for(engine.tick(), .5)
        assert store.kv_get('heartbeat') == 1000
        assert any(w.chat and w.agent.id == 'pm' for w in launched)
        assert not any(w.agent.id == 'lead' for w in launched)
        engine.mail_triage.forget('lead')
        await engine._merge_task
    asyncio.run(scenario())


def test_new_mail_invalidates_held_batch(project, monkeypatch):
    cfg, store, engine = setup(project, monkeypatch)
    cfg.triage = TriageSettings(enabled=True)
    batches = []
    async def hold(cfg, agent, messages, task):
        batches.append(len(messages))
        return {'wake_now': False, 'reason': 'info', 'digest': 'info'}
    monkeypatch.setattr('troupe.triage.classify', hold)
    async def scenario():
        mail(store)
        reasons(engine)
        await settled(engine)
        mail(store)
        reasons(engine)
        await settled(engine)
        assert batches == [1, 2]
    asyncio.run(scenario())


def test_hour_pattern_replay_reduces_message_wakes_at_least_half(project, monkeypatch):
    # Synthetic replay of 28 coordination arrivals: 24 status updates + 4 questions.
    cfg, store, engine = setup(project, monkeypatch)
    cfg.triage = TriageSettings(enabled=True, max_pending=5)
    async def hold(*args):
        return {'wake_now': False, 'reason': 'info', 'digest': 'Status only'}
    monkeypatch.setattr('troupe.triage.classify', hold)
    async def replay():
        wakes = 0
        for i in range(28):
            store.send('pm', 'lead', 'Can you decide?' if i % 7 == 6 else 'Status delivered', fyi=i % 7 < 3)
            store.x('UPDATE messages SET ts=990')
            reasons(engine)
            await settled(engine)
            if 'messages' in reasons(engine):
                wakes += 1
                store.mark_read([m['id'] for m in store.unread('lead')])
                engine.mail_triage.forget('lead')
        assert wakes <= 14
        assert wakes == 8  # count guard batches FYIs, questions always wake
    asyncio.run(replay())


def test_additive_schema_legacy_mail_defaults_to_actionable(tmp_path):
    db = tmp_path / 'legacy.db'
    from troupe.store import SCHEMA
    conn = sqlite3.connect(db)
    conn.executescript(SCHEMA)
    conn.execute("INSERT INTO messages(sender,recipient,body) VALUES('pm','lead','old')")
    conn.commit()
    conn.close()
    store = Store(db)
    assert store.messages()[0]['fyi'] == 0


@pytest.mark.parametrize('settings', [{'max_pending': 0}, {'timeout': float('nan')}, {'enabled': 'yes'}])
def test_invalid_settings_rejected(settings):
    with pytest.raises(ValueError):
        TriageSettings(**settings)


def test_model_protocol_and_malformed_reply(monkeypatch, project):
    cfg, store = project
    cfg.triage = TriageSettings(enabled=True, model='triage-small')
    replies = [{'wake_now': False, 'reason': 'Informational', 'digest': 'Build finished'},
               {'wake_now': 'false', 'reason': 'bad', 'digest': ''}]
    requests = []
    class Response:
        def raise_for_status(self):
            pass
        def json(self):
            return {'choices': [{'message': {'content': json.dumps(replies.pop(0))}}]}
    class Client:
        def __init__(self, **kw):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass
        async def post(self, url, **kw):
            requests.append((url, kw['json']))
            return Response()
    monkeypatch.setattr('troupe.triage.httpx.AsyncClient', Client)
    mail(store)
    async def scenario():
        result = await classify(cfg, cfg.agent('lead'), store.unread('lead'), None)
        assert result['wake_now'] is False
        with pytest.raises(ValueError, match='Malformed'):
            await classify(cfg, cfg.agent('lead'), store.unread('lead'), None)
    asyncio.run(scenario())
    assert requests[0][0].endswith('/chat/completions')
    assert requests[0][1]['model'] == 'triage-small'
    assert json.loads(requests[0][1]['messages'][1]['content'])['role'] == 'lead'


def test_coordination_work_metric_excludes_chat(project):
    from troupe.gui.data import Data
    cfg, store = project
    for reason, task, chat, tokens in [('messages', None, False, 100), ('messages', 1, False, 200),
                                       ('review', 2, False, 300), ('chat', None, True, 900)]:
        rid = store.start_run('lead', reason, task, str(cfg.root), chat)
        store.end_run(rid, 'ok', 0, tokens, '')
    data = Data(cfg)
    data.refresh(force=True)
    assert data.work_tokens_1h == {'coordination': 100, 'work': 500}


def test_triage_config_loads(project):
    cfg, store = project
    path = cfg.state_dir / config.CONFIG_FILE
    path.write_text(path.read_text().replace('enabled = false', 'enabled = true')
                    .replace('max_pending = 5', 'max_pending = 7'))
    updated = config.load(cfg.root)
    assert updated.triage.enabled and updated.triage.max_pending == 7
