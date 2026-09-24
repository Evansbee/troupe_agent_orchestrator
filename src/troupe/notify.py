"""One persistent, coalescing OS notifier per project service."""
from __future__ import annotations

import asyncio
import subprocess
import time
from datetime import datetime

from .store import HandleBook, Store

KINDS = {'question', 'blocked', 'check_failed', 'rate_limit', 'providers',
         'backoff', 'crash_loop', 'throttle', 'safety', 'chat', 'stalled', 'timeout', 'concern'}
FOCUS_TTL = 5
BATCH_DELAY = 3
INTERVAL = 30


def deliver(title: str, body: str) -> bool:
    # Values are argv, never AppleScript source (quotes/backslashes/newlines are data).
    script = 'on run argv\ndisplay notification (item 2 of argv) with title (item 1 of argv)\nend run'
    try:
        result = subprocess.run(['osascript', '-e', script, title, body],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=3)
        return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


class Notifier:
    def __init__(self, store: Store, deliver_fn=deliver, clock=time.time):
        self.store, self.deliver, self.clock = store, deliver_fn, clock
        self.state = store.kv_get('notify.state', None) or {
            'seen': [], 'pending': {}, 'last_sent': 0, 'cursor': store.max_event_id(),
            'limits': {}, 'throttle': '', 'throttle_episode': 0,
        }

    def add(self, key: str, kind: str, agent: str, text: str, *, urgent=False) -> None:
        if key not in self.state['seen']:
            pending = self.state['pending']
            if key not in pending and kind not in ('question', 'chat'):
                self.store.event(agent, 'needs_help', text, ref=key, significant=False)
            pending[key] = dict(kind=kind, agent=agent, text=text, urgent=urgent,
                                since=pending.get(key, {}).get('since', self.clock()))

    def collect(self, cfg, failures) -> None:
        s, state, stamp = self.store, self.state, self.clock()
        for q in s.questions(limit=1000000):
            self.add(f"q:{q['id']}", 'question', q['asker'], q['question'])
        for t in s.tasks(limit=1000000):
            if t['status'] == 'blocked':
                self.add(f"blocked:{t['id']}", 'blocked', t['assignee'] or 'system',
                         f"Task #{t['id']} {t['title']} is blocked", urgent=bool(t.get('human_request')))
            if (s.kv_get(f"check_failures.{t['id']}", 0) or 0) >= 2:
                self.add(f"check:{t['id']}", 'check_failed', t['assignee'] or 'system',
                         f"Task #{t['id']} {t['title']} failed its merge check twice")
        for backend in ('claude', 'codex', 'local'):
            reset = s.kv_get(f'limit.{backend}', 0) or 0
            window = state['limits'].get(backend)
            if reset > stamp:
                if not window or window['until'] <= stamp:
                    window = dict(key=f'limit:{backend}:{stamp}', until=reset)
                window['until'] = reset
                state['limits'][backend] = window
                agent = next((a.id for a in cfg.agents if a.backend == backend), 'system')
                self.add(window['key'], 'rate_limit', agent,
                         f"{backend.title()} limited until {datetime.fromtimestamp(reset):%H:%M}")
            else:
                state['limits'].pop(backend, None)
        for agent, (count, _) in failures.items():
            if count >= 6:  # min(600, 30 * 2 ** (count - 1)) reached the cap
                self.add(f'backoff:{agent}', 'backoff', agent, 'Repeated runs failed; retry backoff reached its cap')
        throttle = s.kv_get('throttled', '') or ''
        if throttle and not state['throttle']:
            state['throttle_episode'] += 1
        state['throttle'] = throttle
        if throttle:
            self.add(f"throttle:{state['throttle_episode']}", 'throttle', 'system', throttle)
        # Read ascending so busy projects never skip events across bounded pages.
        for e in s.q('SELECT * FROM events WHERE id>? ORDER BY id LIMIT 1000', state['cursor']):
            state['cursor'] = e['id']
            if e['kind'] == 'concern':
                # REQ-COM-029: content-free and reporter-free, same as the feed — 'system' as the
                # agent (never the real reporter) keeps the notification title/body from leaking who
                # filed it, exactly like the feed text itself carries no content or reporter.
                self.add(f"event:{e['id']}", 'concern', 'system',
                        'An agent raised a concern. Run `troupe concerns` to read it.')
            elif e['kind'] in ('safety', 'crash_loop', 'providers', 'stalled', 'timeout'):
                self.add(f"event:{e['id']}", e['kind'], e['agent'], e['text'])
            elif e['kind'] == 'message' and e['ref'].startswith('msg:'):
                m = s.one('SELECT * FROM messages WHERE id=?', int(e['ref'][4:]))
                if m and m['recipient'] == 'human' and m['kind'] == 'chat':
                    self.add(f"msg:{m['id']}", 'chat', m['sender'], m['body'])

    def tick(self, cfg, *, api_suppressed=False, failures=None) -> None:
        self.collect(cfg, failures or {})
        state, stamp = self.state, self.clock()
        focus = self.store.kv_get('gui_focused_at', 0) or 0
        suppressed = api_suppressed or (focus > 0 and stamp - focus < FOCUS_TTL)
        pending = state['pending']
        for key, item in list(pending.items()):
            if item['kind'] == 'concern':
                # REQ-COM-029: must-deliver. troupe.toml is agent-writable, so neither
                # notify.enabled nor notify.quiet may hold this back — NotifySettings also rejects
                # "concern" in quiet outright; this is the belt-and-suspenders half.
                continue
            if suppressed or not cfg.notify.enabled or item['kind'] in cfg.notify.quiet:
                state['seen'].append(key)
                del pending[key]  # already visible in-window or owned by the native client
        urgent = any(item['urgent'] for item in pending.values())
        ready = pending and stamp - min(item['since'] for item in pending.values()) >= BATCH_DELAY
        if pending and (urgent or (ready and stamp >= state['last_sent'] + INTERVAL)):
            names = HandleBook(cfg.project, cfg.agents)
            items = sorted(pending.values(), key=lambda item: (not item['urgent'], item['since']))
            title = (f"{names.name(items[0]['agent'])} needs you" if len(items) == 1
                     else f"{len(items)} things need you in {cfg.project}")
            if items[0]['agent'] == 'system' and len(items) == 1:
                title = f'{cfg.project} needs you'
            body = '\n'.join(f"{names.name(i['agent'])}: {i['text'][:180]}" for i in items[:3])
            state['last_sent'] = stamp  # failed delivery retries are also rate limited
            if self.deliver(title, body):
                state['seen'].extend(pending)
                pending.clear()
            else:
                import logging
                logging.getLogger(__name__).warning('OS notification delivery failed; pending batch retained')
        self.store.kv_set('notify.state', state)

    async def run(self, engine) -> None:
        while not engine._stop.is_set():
            try:
                await asyncio.to_thread(self.tick, engine.cfg,
                    api_suppressed=engine.api.notifications_suppressed,
                    failures=dict(engine.failures))
            except Exception:
                # Notification delivery must not take down the team.
                import logging
                logging.getLogger(__name__).exception('Notification polling failed')
            await asyncio.sleep(1)
