import asyncio
import json
import os
import sys
import time
from pathlib import Path

import pytest

from troupe.config import AgentCfg, Config
from troupe.engine import Engine, Wake
from troupe.runners import CodexRunner, RunResult
from troupe.store import Store
from troupe.usage import app_server_usage, apply_usage, limited_until, normalize, rollout_usage

SID = '01990000-1234-5678-abcd-000000000001'


def limits(percent=25, minutes=300, reset=2000000000, secondary=None):
    return dict(limit_id='codex', primary=dict(used_percent=percent, window_minutes=minutes, resets_at=reset),
                secondary=secondary, plan_type='pro', rate_limit_reached_type=None)


def write_rollout(home, raw, stamp='2026-09-24T05:30:00Z'):
    path = home / 'sessions/2026/09/24' / f'rollout-date-{SID}.jsonl'
    path.parent.mkdir(parents=True, exist_ok=True)
    event = dict(timestamp=stamp, type='event_msg', payload=dict(type='token_count', rate_limits=raw))
    path.write_text(json.dumps({'type': 'response_item', 'payload': {'text': 'PRIVATE CONVERSATION'}}) + '\n' +
                    '{malformed\n' + json.dumps(event) + '\n' + '[]\n' + '{"partial":')
    return path


@pytest.mark.parametrize('secondary', [None, dict(used_percent=61, window_minutes=10080, resets_at=2000000400)])
def test_rollout_allowlist_windows_and_malformed_lines(tmp_path, secondary):
    raw = limits(secondary=secondary)
    raw['credits'] = {'secret': 'PRIVATE CREDIT'}
    raw['primary']['private'] = 'PRIVATE WINDOW'
    write_rollout(tmp_path, raw)
    sample = rollout_usage([SID], tmp_path)
    assert sample['unifiedWindows']['five_hour']['used_percent'] == 25
    assert sample['plan_type'] == 'pro'
    assert ('seven_day' in sample['unifiedWindows']) == bool(secondary)
    assert 'PRIVATE' not in json.dumps(sample)
    assert rollout_usage(['../../outside'], tmp_path) is None
    assert rollout_usage(['01990000-1234-5678-abcd-000000000002'], tmp_path) is None


def test_primary_weekly_not_mislabeled_and_timestamp_order(tmp_path):
    path = write_rollout(tmp_path, limits(11,10080))
    with path.open('a') as f:
        f.write('\n' + json.dumps(dict(type='event_msg', timestamp='2020-01-01T00:00:00Z',
                    payload=dict(type='token_count', rate_limits=limits(99,10080)))) + '\n')
    sample = rollout_usage([SID], tmp_path)
    assert list(sample['unifiedWindows']) == ['seven_day']
    assert sample['unifiedWindows']['seven_day']['used_percent'] == 11
    assert normalize(limits(minutes=15), 1, 'rollout')['unifiedWindows']['window_15']


def test_backoff_reported_expiry_and_stale_samples(tmp_path, monkeypatch):
    (tmp_path / '.troupe').mkdir()
    cfg = Config(tmp_path, 'Test', [AgentCfg('lead', 'lead', 'Lead', 'codex')])
    e = Engine(cfg)
    monkeypatch.setattr('troupe.usage.time.time', lambda: 1000)
    monkeypatch.setattr('troupe.engine.now', lambda: 1000)
    e.record_limit('codex', 1900, reported=False)
    sample = normalize(limits(100, reset=1120), 1000, 'rollout')
    apply_usage(e.store, sample, e.record_limit)
    assert e.store.kv_get('limit.codex') == 1120
    assert e.backend_limited('codex')
    monkeypatch.setattr('troupe.engine.now', lambda: 1120)
    assert not e.backend_limited('codex')
    assert limited_until(sample, 1120) is None
    raw = limits(20, reset=1130);raw['rate_limit_reached_type'] = 'primary'
    assert limited_until(normalize(raw, 1000, 'rollout'), 1000) == 1130
    raw['primary']['resets_at'] = None
    sample = normalize(raw, 1000, 'rollout')
    assert limited_until(sample, 1000) == 1900
    assert limited_until(sample, 1900) is None
    assert 'PRIVATE' not in json.dumps(e.store.kv_get('usage:codex'))


def test_both_exhausted_windows_wait_until_last_reset():
    raw = limits(100, reset=1100, secondary=dict(used_percent=100,window_minutes=10080,resets_at=1200))
    assert limited_until(normalize(raw, 1000, 'rollout'),1000) == 1200


def fake_server(tmp_path, body):
    path = tmp_path / 'server'
    path.write_text(f'#!{sys.executable}\n'+body)
    path.chmod(0o700)
    return str(path)


def test_rpc_handshake_and_allowlisted_response(tmp_path):
    command = fake_server(tmp_path, '''import sys,json
for line in sys.stdin:
 o=json.loads(line)
 if o.get('method')=='initialize': print(json.dumps({'id':1,'result':{}}),flush=True)
 elif o.get('method')=='account/rateLimits/read':
  print(json.dumps({'id':2,'result':{'private':'PRIVATE','rateLimitsByLimitId':{'codex':{'primary':{'usedPercent':12,'windowDurationMins':10080,'resetsAt':2000000000},'planType':'pro','credits':{'private':'PRIVATE'}}}}}),flush=True)
''')
    home = tmp_path / 'isolated-home'
    sample = asyncio.run(app_server_usage(command, tmp_path, home))
    assert sample['source'] == 'app-server'
    assert sample['unifiedWindows']['seven_day']['used_percent'] == 12
    assert 'PRIVATE' not in json.dumps(sample)


def test_rpc_runs_under_the_isolated_codex_home_not_the_humans(tmp_path, monkeypatch):
    """REQ-BE-015 integration: the RPC subprocess's CODEX_HOME must be the agent's isolated home,
    never whatever ~/.codex or an inherited CODEX_HOME the engine process happens to have."""
    monkeypatch.setenv('CODEX_HOME', str(tmp_path / 'the-humans-real-codex-home'))
    seen = tmp_path / 'seen_codex_home'
    ok_result = json.dumps({'id': 2, 'result': {'rateLimitsByLimitId': {'codex': {
        'primary': {'usedPercent': 1, 'windowDurationMins': 300, 'resetsAt': 2000000000}}}}})
    script = (
        "import os, sys, json\n"
        f"open({str(seen)!r}, 'w').write(os.environ.get('CODEX_HOME', ''))\n"
        "for line in sys.stdin:\n"
        " o = json.loads(line)\n"
        " if o.get('method') == 'initialize':\n"
        "  print(json.dumps({'id': 1, 'result': {}}), flush=True)\n"
        " elif o.get('method') == 'account/rateLimits/read':\n"
        f"  print({ok_result!r}, flush=True)\n"
    )
    command = fake_server(tmp_path, script)
    home = tmp_path / 'isolated-home'
    sample = asyncio.run(app_server_usage(command, tmp_path, home))
    assert sample is not None
    assert seen.read_text() == str(home)


def test_rpc_timeout_reaps_process(tmp_path,monkeypatch):
    import troupe.usage as usage
    pidfile=tmp_path/'pid'
    command=fake_server(tmp_path,f'import os,time\nfrom pathlib import Path\nPath({str(pidfile)!r}).write_text(str(os.getpid()))\ntime.sleep(30)\n')
    monkeypatch.setattr(usage,'RPC_TIMEOUT',.2)
    assert asyncio.run(app_server_usage(command, tmp_path, tmp_path / 'isolated-home')) is None
    with pytest.raises(ProcessLookupError):
        os.kill(int(pidfile.read_text()),0)


def test_post_run_updates_store_without_logging_conversation(tmp_path, monkeypatch):
    from troupe.runners import codex_home_dir
    (tmp_path/'.troupe').mkdir()
    cfg=Config(tmp_path,'Test',[AgentCfg('lead','lead','Lead','codex')]);cfg.git_autocommit=False
    home = codex_home_dir(cfg, 'lead')
    write_rollout(home,limits(100,reset=time.time()+200))
    e=Engine(cfg);e.store.sync_agents(cfg.agents)
    async def once(*args):return RunResult(ok=True,session_id=SID)
    monkeypatch.setattr(CodexRunner,'_run_once',once)
    async def run():
        await e.launch(Wake(0,cfg.agents[0],'chat'))
        await e.running['lead'][1]
    asyncio.run(run())
    sample=e.store.kv_get('usage:codex')
    assert sample['unifiedWindows']['five_hour']['used_percent']==100
    assert e.backend_limited('codex')
    assert 'PRIVATE' not in json.dumps(e.store.q('SELECT * FROM kv')+e.store.q('SELECT * FROM run_lines'))


def test_post_run_hook_survives_a_concurrent_sweep_zombie_runs(tmp_path, monkeypatch):
    """QA #52 repro: the hook awaits (a thread hop, so it yields to the event loop), and #61's
    sweep_zombie_runs() runs every tick — it flips any `runs` row still 'running' whose agent
    isn't in engine.running to 'interrupted' and requeues its mail. If that ran mid-await, on the
    version of this hook that sat between running.pop() and end_run(), a just-finished run would
    get its mail wrongly requeued. Placing the hook after end_run()/set_agent() means the row and
    the agent's mail are already finalized before the hook's first await, so a sweep landing
    exactly then must be a no-op for this run."""
    from troupe.runners import codex_home_dir
    (tmp_path / '.troupe').mkdir()
    cfg = Config(tmp_path, 'Test', [AgentCfg('lead', 'lead', 'Lead', 'codex')])
    cfg.git_autocommit = False
    home = codex_home_dir(cfg, 'lead')
    write_rollout(home, limits())
    e = Engine(cfg)
    e.store.sync_agents(cfg.agents)
    e.store.send('human', 'lead', 'hello', kind='chat')
    msg_id = e.store.unread('lead')[0]['id']

    async def once(*args):
        return RunResult(ok=True, session_id=SID)
    monkeypatch.setattr(CodexRunner, '_run_once', once)

    import troupe.usage as usage
    real_rollout_usage = usage.rollout_usage
    swept = []

    def spying_rollout_usage(*args, **kwargs):
        swept.append(e.sweep_zombie_runs())  # simulates a tick landing during the hook's await
        return real_rollout_usage(*args, **kwargs)
    monkeypatch.setattr(usage, 'rollout_usage', spying_rollout_usage)

    async def run():
        await e.launch(Wake(0, cfg.agents[0], 'chat'))
        await e.running['lead'][1]
    asyncio.run(run())

    assert swept, 'the sweep must actually have run during the hook for this test to mean anything'
    run_row = e.store.one('SELECT * FROM runs WHERE agent=?', 'lead')
    assert run_row['status'] == 'ok'
    assert e.store.one('SELECT read_at FROM messages WHERE id=?', msg_id)['read_at'] is not None


def test_post_run_ignores_a_foreign_agents_codex_home(tmp_path, monkeypatch):
    """A rollout under another agent's isolated home (or the human's real ~/.codex) must not leak
    into this agent's usage sample — each agent's post-run hook only ever searches its own home."""
    from troupe.runners import codex_home_dir
    (tmp_path/'.troupe').mkdir()
    cfg=Config(tmp_path,'Test',[AgentCfg('lead','lead','Lead','codex')]);cfg.git_autocommit=False
    write_rollout(codex_home_dir(cfg, 'someone-else'), limits(100, reset=time.time()+200))
    e=Engine(cfg);e.store.sync_agents(cfg.agents)
    async def once(*args):return RunResult(ok=True,session_id=SID)
    monkeypatch.setattr(CodexRunner,'_run_once',once)
    async def run():
        await e.launch(Wake(0,cfg.agents[0],'chat'))
        await e.running['lead'][1]
    asyncio.run(run())
    assert e.store.kv_get('usage:codex') is None


def test_monitor_fallback_and_minute_timer(tmp_path,monkeypatch):
    import troupe.usage as usage
    from troupe.runners import codex_home_dir
    (tmp_path/'.troupe').mkdir()
    cfg=Config(tmp_path,'Test',[AgentCfg('lead','lead','Lead','codex')]);e=Engine(cfg);e.store.sync_agents(cfg.agents)
    e.store.set_agent('lead',session_id=SID)
    sample=normalize(limits(),time.time(),'rollout');calls=[]
    async def rpc(*args):calls.append('rpc');return None
    def fallback(ids,home):assert ids==[SID];assert home==codex_home_dir(cfg,'lead');return sample
    async def sleep(seconds):assert seconds==60;e._stop.set()
    monkeypatch.setattr(usage,'app_server_usage',rpc);monkeypatch.setattr(usage,'rollout_usage',fallback)
    monkeypatch.setattr(usage.asyncio,'sleep',sleep)
    asyncio.run(usage.monitor(e))
    assert calls==['rpc'] and e.store.kv_get('usage:codex')==sample
    assert (cfg.state_dir / 'codex-home' / 'lead' / 'config.toml').exists()


def test_missing_bad_data_preserves_cache(tmp_path):
    s=Store(tmp_path/'db');sample=normalize(limits(),1000,'rollout');s.kv_set('usage:codex',sample)
    apply_usage(s,None,lambda *a,**k:None)
    apply_usage(s,normalize(limits(99),999,'rollout'),lambda *a,**k:None)
    assert s.kv_get('usage:codex')==sample
    for raw in ([],{'primary':[]},limits(percent=float('nan')),limits(minutes=0),{'limit_id':'other',**{'primary':limits()['primary']}}):
        assert normalize(raw,1000,'rollout') is None
