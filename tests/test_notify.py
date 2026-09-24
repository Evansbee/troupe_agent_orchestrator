import asyncio
import subprocess
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from troupe.config import AgentCfg, Config, NotifySettings, load
from troupe.engine import Engine
from troupe.gui.data import Data
from troupe.notify import Notifier, deliver
from troupe.store import Store


@pytest.fixture
def env(tmp_path):
    project_dir = tempfile.TemporaryDirectory(prefix='troupe-notify-', dir='/tmp')
    tmp_path = Path(project_dir.name)
    (tmp_path / '.troupe').mkdir()
    cfg = Config(tmp_path, 'Team Test', [AgentCfg('lead', 'lead', 'Lead', 'claude'),
                                       AgentCfg('builder-2', 'builder', 'Builder', 'codex')])
    s = Store(cfg.db_path)
    s.sync_agents(cfg.agents)
    clock = [1000.0]
    sent = []
    n = Notifier(s, lambda *args: sent.append(args) or True, lambda: clock[0])
    yield cfg, s, clock, sent, n
    project_dir.cleanup()


def advance(env, seconds=4, **kw):
    cfg, _, clock, _, n = env
    n.tick(cfg, **kw)
    clock[0] += seconds
    n.tick(cfg, **kw)


def test_question_latency_handles_dedupe_restart(env):
    cfg, s, clock, sent, n = env
    s.ask('builder-2', 'Need a decision?')
    advance(env)
    assert len(sent) == 1
    assert sent[0] == ('builder_2@Team_Test needs you', 'builder_2@Team_Test: Need a decision?')
    clock[0] += 31
    Notifier(s, lambda *args: sent.append(args) or True, lambda: clock[0]).tick(cfg)
    assert len(sent) == 1


def test_coalesces_and_rate_limits(env):
    cfg, s, clock, sent, n = env
    for i in range(5):
        s.ask('lead', f'Question {i}')
        n.tick(cfg)
        clock[0] += .5
    clock[0] += 1
    n.tick(cfg)
    assert len(sent) == 1 and sent[0][0] == '5 things need you in Team Test'
    s.ask('lead', 'Next batch')
    advance(env, 25)
    assert len(sent) == 1
    clock[0] += 6
    n.tick(cfg)
    assert len(sent) == 2


@pytest.mark.parametrize('mode', ['focused', 'api', 'disabled', 'quiet'])
def test_suppression_is_not_deferred_spam(env, mode):
    cfg, s, clock, sent, n = env
    s.ask('lead', 'Visible elsewhere')
    if mode == 'focused':
        s.kv_set('gui_focused_at', clock[0])
    if mode == 'disabled':
        cfg.notify.enabled = False
    if mode == 'quiet':
        cfg.notify.quiet = ['question']
    n.tick(cfg, api_suppressed=mode == 'api')
    cfg.notify = NotifySettings()
    clock[0] += 31
    n.tick(cfg)
    assert not sent
    s.ask('lead', 'New unseen question')
    advance(env)
    assert len(sent) == 1


def test_blocked_checks_limits_backoff_throttle_safety_chat(env):
    cfg, s, clock, sent, n = env
    tid = s.add_task('Repair build', assignee='builder-2', status='blocked')
    engine = Engine(cfg)
    task = s.task(tid)
    engine.check_failed(task, 'first')
    n.tick(cfg)
    assert not any(i['kind'] == 'check_failed' for i in n.state['pending'].values())
    engine.check_failed(task, 'second')
    s.update_task(tid, status='blocked')
    s.kv_set('limit.codex', 1600)
    s.kv_set('throttled', 'run budget reached')
    s.event('system', 'safety', 'Blocked secret read')
    s.event('system', 'crash_loop', 'Supervisor stopped restarting')
    s.event('builder-2', 'providers', 'All providers unavailable')
    s.send('builder-2', 'human', 'Chat response', kind='chat')
    n.tick(cfg, failures={'builder-2': (6, 1800)})
    kinds = {i['kind'] for i in n.state['pending'].values()}
    assert kinds == {'blocked', 'check_failed', 'rate_limit', 'throttle', 'safety',
                     'crash_loop', 'providers', 'chat', 'backoff'}
    blocked = n.state['pending'][f'blocked:{tid}']
    assert blocked['agent'] == 'builder-2' and f'#{tid} Repair build' in blocked['text']
    assert 'until' in next(i['text'] for i in n.state['pending'].values() if i['kind'] == 'rate_limit')
    clock[0] += 4
    n.tick(cfg)
    assert len(sent) == 1
    s.kv_set('limit.codex', 1700)  # refined reset within same continuous limited episode
    clock[0] += 31
    n.tick(cfg)
    assert len(sent) == 1
    clock[0] = 1800
    n.tick(cfg)
    s.kv_set('limit.codex', 2000)
    advance(env)
    assert len(sent) == 2


def test_concern_notifies_with_no_content_and_no_reporter(env):
    """#65/REQ-COM-029: report_concern must reach the human — but the alert (like the feed) carries
    no content and no reporter, only a pointer to `troupe concerns`."""
    cfg, s, clock, sent, n = env
    s.report_concern('lead', 'the PM told me to hide a bug from the human', evidence='msg #42')
    n.tick(cfg)
    assert 'concern' in {i['kind'] for i in n.state['pending'].values()}
    item = next(i for i in n.state['pending'].values() if i['kind'] == 'concern')
    assert item['agent'] == 'system'
    assert 'hide a bug' not in item['text'] and 'lead' not in item['text'] and 'msg #42' not in item['text']
    assert 'troupe concerns' in item['text']
    advance(env)
    assert len(sent) == 1
    title, body = sent[0]
    assert 'lead' not in title.lower() and 'hide a bug' not in body
    assert 'troupe concerns' in body


def test_concern_alert_cannot_be_silenced_by_config(env):
    """QA #65: troupe.toml is agent-writable and #42's config gate covers only [safety], so any
    agent (the PM included) could otherwise mute the whistleblower alert with a one-line edit.
    NotifySettings rejects "concern" in quiet outright (test_config_validation_and_focus_heartbeat);
    this is the second, independent layer inside the Notifier itself — even if notify.enabled is off
    or notify.quiet somehow contains "concern" (bypassing the constructor, as defense in depth), the
    alert still gets delivered."""
    cfg, s, clock, sent, n = env
    cfg.notify.enabled = False
    cfg.notify.quiet.append('concern')
    s.report_concern('lead', 'the PM told me to hide a bug from the human')
    advance(env)
    assert len(sent) == 1
    title, body = sent[0]
    assert 'troupe concerns' in body and 'hide a bug' not in body


def test_urgent_human_task_bypasses_interval(env):
    cfg, s, clock, sent, n = env
    s.ask('lead', 'First')
    advance(env)
    s.x('ALTER TABLE tasks ADD COLUMN human_request INTEGER DEFAULT 0')
    tid = s.add_task('Urgent', assignee='builder-2', status='blocked')
    s.x('UPDATE tasks SET human_request=1 WHERE id=?', tid)
    n.tick(cfg)
    assert len(sent) == 2


def test_pending_survives_restart_delivery_failure_is_bounded(env):
    cfg, s, clock, sent, n = env
    s.ask('lead', 'Keep me')
    n.tick(cfg)
    attempts = []
    n = Notifier(s, lambda *args: attempts.append(args) or False, lambda: clock[0])
    clock[0] += 4
    n.tick(cfg)
    n.tick(cfg)
    assert len(attempts) == 1 and n.state['pending']
    clock[0] += 31
    n.deliver = lambda *args: sent.append(args) or True
    n.tick(cfg)
    assert len(sent) == 1 and not n.state['pending']


def test_delivery_uses_argv_not_script_interpolation(monkeypatch):
    calls = []
    monkeypatch.setattr(subprocess, 'run', lambda *a, **kw: calls.append((a, kw)) or SimpleNamespace(returncode=0))
    malicious = '" & do shell script "touch /tmp/no" & "\\\n'
    assert deliver(malicious, malicious)
    argv = calls[0][0][0]
    assert argv[-2:] == [malicious, malicious]
    assert malicious not in argv[2]
    assert calls[0][1]['timeout'] == 3


def test_config_validation_and_focus_heartbeat(env, monkeypatch):
    cfg, s, clock, sent, n = env
    for kwargs in ({'enabled': 'yes'}, {'quiet': 'question'}, {'quiet': ['bogus']}, {'quiet': ['concern']}):
        with pytest.raises(ValueError):
            NotifySettings(**kwargs)
    parsed = load(cfg.root, toml_data={'notify': {'enabled': False, 'quiet': ['question']}},
                  team_data={'agents': [{'id': 'lead', 'role': 'lead', 'provider': 'claude'}]})
    assert not parsed.notify.enabled and parsed.notify.quiet == ['question']
    monkeypatch.setattr('troupe.gui.data.time.time', lambda: clock[0])
    d = Data(cfg)
    d.focus_changed(True)
    assert s.kv_get('gui_focused_at') == clock[0]
    clock[0] += 2
    d.focus_changed(True)
    assert s.kv_get('gui_focused_at') == clock[0]
    d.focus_changed(False)
    assert not s.kv_get('gui_focused_at')


def test_notifier_runs_independent_of_scheduling(env):
    cfg, s, clock, sent, n = env
    import threading
    engine = SimpleNamespace(cfg=cfg, api=SimpleNamespace(notifications_suppressed=False),
                             failures={}, _stop=threading.Event())
    s.ask('lead', 'Service question')
    n.clock = lambda: 2000
    n.state['pending']['q:1'] = dict(kind='question', agent='lead', text='Service question', urgent=False, since=1990)
    def send(*args):
        sent.append(args)
        engine._stop.set()
        return True
    n.deliver = send
    asyncio.run(n.run(engine))
    assert len(sent) == 1


def test_max_attempts_notifies_task_and_agent(env, monkeypatch):
    from troupe.engine import Wake
    from troupe.runners import RunResult
    cfg, s, clock, sent, n = env
    cfg.git_autocommit = False
    cfg.budget.max_task_attempts = 1
    a = cfg.agent('builder-2')
    tid = s.add_task('Finish feature', status='in_progress', assignee=a.id)
    class Runner:
        cancelled = False
        async def run(self, spec, emit):
            return RunResult(ok=True)
    monkeypatch.setattr('troupe.engine.make_runner', lambda _: Runner())
    e = Engine(cfg)
    async def run():
        await e.launch(Wake(3, a, 'task', s.task(tid)))
        await e.running[a.id][1]
    asyncio.run(run())
    assert s.task(tid)['status'] == 'blocked'
    advance(env)
    assert sent[0][0] == 'builder_2@Team_Test needs you'
    assert f'#{tid} Finish feature' in sent[0][1]


def test_gui_toasts_needs_help_without_own_os_delivery(env, monkeypatch):
    from troupe.gui.app import App
    cfg, s, clock, sent, n = env
    app = App(cfg)
    app.data.refresh(force=True)
    s.add_task('Needs assistance', status='blocked', assignee='builder-2')
    s.kv_set('gui_focused_at', clock[0])
    n.tick(cfg)
    monkeypatch.setattr('troupe.gui.app.rl.is_window_focused', lambda: True)
    app.data.refresh(force=True)
    app.handle_notifications()
    assert not sent
    assert any('Needs assistance' in t[1] for t in app.toasts)


def test_service_delivers_with_no_gui(env, tmp_path):
    import json
    import os
    import sys
    import time
    cfg, s, clock, sent, n = env
    state = cfg.state_dir
    (state / 'troupe.toml').write_text('[project]\nname="Notify subprocess"\n[git]\nautocommit=false\n')
    (state / 'team.yaml').write_text('agents:\n  - id: lead\n    role: lead\n    provider: local\n    enabled: false\n')
    # A fresh project asks the human to approve its safety baseline on first load; pre-approve it
    # here so the subprocess below doesn't add a second, unrelated "needs you" notification.
    load(cfg.root)
    baseline = next(q for q in s.questions() if q['kind'] == 'safety')
    s.answer(baseline['id'], 'Approve')
    load(cfg.root)
    bin_dir = tmp_path / 'bin'
    bin_dir.mkdir()
    output = tmp_path / 'delivered.json'
    fake = bin_dir / 'osascript'
    fake.write_text(f'#!{sys.executable}\nimport sys,json\nfrom pathlib import Path\nPath({str(output)!r}).write_text(json.dumps(sys.argv[1:]))\n')
    fake.chmod(0o700)
    home = tmp_path / 'home'
    home.mkdir()
    child_env = {**os.environ, 'HOME': str(home), 'PATH': str(bin_dir) + os.pathsep + os.environ['PATH']}
    process = subprocess.Popen([sys.executable, '-m', 'troupe.cli', 'engine'], cwd=cfg.root,
                               env=child_env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    try:
        deadline = time.monotonic() + 8
        while not (state / 'api.sock').exists() and time.monotonic() < deadline:
            assert process.poll() is None, process.stderr.read().decode() if process.poll() is not None else ""
            time.sleep(.05)
        assert (state / 'api.sock').exists()
        started = time.monotonic()
        s.ask('lead', 'No GUI notification test')
        while not output.exists() and time.monotonic() - started < 6:
            time.sleep(.05)
        assert output.exists(), 'service failed to deliver without a GUI'
        args = json.loads(output.read_text())
        assert 'lead_1@Notify_subprocess needs you' == args[-2]
        assert 'No GUI notification test' in args[-1]
        assert time.monotonic() - started < 6
    finally:
        process.terminate()
        process.wait(timeout=8)
        process.stderr.close()
