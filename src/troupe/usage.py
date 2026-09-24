"""Codex account quotas, without retaining session content or credentials."""
from __future__ import annotations

import asyncio
import json
import math
import os
import re
import time
from datetime import datetime
from pathlib import Path

REFRESH_SECONDS = 60
RPC_TIMEOUT = 5
TOKEN_EVENT = re.compile(rb'"type"\s*:\s*"token_count"')
SESSION_ID = re.compile(r'[a-fA-F0-9]{8}(?:-[a-fA-F0-9]{4}){3}-[a-fA-F0-9]{12}')


def number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def normalize(raw: object, observed_at: float, source: str) -> dict | None:
    """Allowlist quota fields; never retain credits, account identity or arbitrary payloads."""
    if not isinstance(raw, dict) or raw.get('limit_id', raw.get('limitId', 'codex')) not in ('codex', None):
        return None
    windows = {}
    for slot in ('primary', 'secondary'):
        w = raw.get(slot)
        if not isinstance(w, dict):
            continue
        used = w.get('used_percent', w.get('usedPercent'))
        minutes = w.get('window_minutes', w.get('windowDurationMins'))
        reset = w.get('resets_at', w.get('resetsAt'))
        if not number(used) or used < 0 or not number(minutes) or minutes <= 0:
            continue
        key = {300: 'five_hour', 10080: 'seven_day'}.get(minutes, f'window_{minutes:g}')
        if key in windows:
            key += '_' + slot
        windows[key] = dict(utilization=used / 100, used_percent=used, window_minutes=minutes,
                            resets_at=reset if number(reset) and reset > 0 else None, slot=slot)
    reached = raw.get('rate_limit_reached_type', raw.get('rateLimitReachedType'))
    # Enum-shaped strings only; do not turn unknown nested fields into persisted text.
    reached = reached if isinstance(reached, str) and re.fullmatch(r'[a-zA-Z_\-]{1,64}', reached) else None
    if not windows and not reached:
        return None
    plan = raw.get('plan_type', raw.get('planType'))
    plan = plan if isinstance(plan, str) and re.fullmatch(r'[a-zA-Z_\-]{1,32}', plan) else None
    return dict(unifiedWindows=windows, plan_type=plan, rate_limit_reached_type=reached,
                observed_at=observed_at, source=source)


def rollout_usage(session_ids: list[str], codex_home: Path) -> dict | None:
    """Search one agent's isolated CODEX_HOME (REQ-BE-015) for the latest rate_limits. Never the
    human's ~/.codex — callers pass the specific agent's home explicitly, no environment fallback."""
    home = codex_home
    latest = None
    for sid in set(session_ids):
        if not isinstance(sid, str) or not SESSION_ID.fullmatch(sid):
            continue
        for path in (home / 'sessions').glob(f'*/*/*/rollout-*-{sid}.jsonl'):
            try:
                # Bound I/O for long sessions. A quota record outside the tail is handled by RPC.
                with path.open('rb') as stream:
                    stream.seek(0, 2)
                    size = stream.tell()
                    stream.seek(max(0, size - 4 * 1024 * 1024))
                    if stream.tell():
                        stream.readline()  # discard partial first line
                    lines = stream.read().splitlines()
                for line in reversed(lines):
                    if not TOKEN_EVENT.search(line):
                        continue
                    try:
                        event = json.loads(line)
                        payload = event.get('payload')
                        if event.get('type') != 'event_msg' or not isinstance(payload, dict) or payload.get('type') != 'token_count':
                            continue
                        stamp = datetime.fromisoformat(event['timestamp'].replace('Z', '+00:00')).timestamp()
                        sample = normalize(payload.get('rate_limits'), stamp, 'rollout')
                    except (ValueError, TypeError, KeyError, AttributeError, OverflowError):
                        continue
                    if sample and (latest is None or stamp > latest['observed_at']):
                        latest = sample
            except OSError:
                continue
    return latest


async def app_server_usage(command: str, cwd: Path, codex_home: Path) -> dict | None:
    """Read-only account/rateLimits/read over the app-server's stdio protocol, run under an isolated
    CODEX_HOME (REQ-BE-015's allowlist config) — never the human's ~/.codex config, plugins or
    connectors."""
    proc = None
    try:
        async with asyncio.timeout(RPC_TIMEOUT):
            env = {**os.environ, 'CODEX_HOME': str(codex_home)}
            proc = await asyncio.create_subprocess_exec(command, 'app-server', '--stdio', cwd=cwd, env=env,
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL, limit=1024 * 1024)
            async def send(obj):
                proc.stdin.write((json.dumps(obj) + '\n').encode())
                await proc.stdin.drain()
            await send({'id': 1, 'method': 'initialize', 'params': {
                'clientInfo': {'name': 'troupe_usage', 'version': '0.1'}}})
            while line := await proc.stdout.readline():
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(event, dict):
                    continue
                if event.get('id') == 1:
                    if 'error' in event:
                        return None
                    await send({'method': 'initialized', 'params': {}})
                    await send({'id': 2, 'method': 'account/rateLimits/read'})
                elif event.get('id') == 2:
                    result = event.get('result') or {}
                    if not isinstance(result, dict):
                        return None
                    buckets = result.get('rateLimitsByLimitId') or {}
                    raw = buckets.get('codex') if isinstance(buckets, dict) else None
                    return normalize(raw or result.get('rateLimits'), time.time(), 'app-server')
    except (OSError, TimeoutError, ValueError):
        return None
    finally:
        if proc and proc.returncode is None:
            try:
                proc.terminate()
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(proc.wait(), 1)
            except TimeoutError:
                proc.kill()
                await proc.wait()
    return None


def limited_until(sample: dict, stamp: float) -> float | None:
    windows = sample.get('unifiedWindows', {})
    reached = sample.get('rate_limit_reached_type')
    exhausted = [w for w in windows.values() if w['used_percent'] >= 100]
    if reached and not exhausted:
        # Match a named window when possible; otherwise every reported reset is a possible blocker.
        selected = [w for k, w in windows.items() if reached in (k, w.get('slot'))]
        exhausted = selected or list(windows.values())
    resets = [w['resets_at'] for w in exhausted if w.get('resets_at') and w['resets_at'] > stamp]
    if resets:
        return max(resets)  # every exhausted window must reset before another run
    # Expired snapshots must never renew a limit. Missing reset gets one fallback from observation time.
    if (exhausted or reached) and not any(w.get('resets_at') for w in exhausted):
        until = sample['observed_at'] + 900
        return until if until > stamp else None
    return None


def apply_usage(store, sample: dict | None, record_limit) -> None:
    if not sample:
        return
    previous = store.kv_get('usage:codex', {}) or {}
    if previous.get('observed_at', 0) > sample['observed_at']:
        return
    store.kv_set('usage:codex', sample)
    until = limited_until(sample, time.time())
    if until:
        reported = any(w.get('resets_at') == until for w in sample['unifiedWindows'].values())
        record_limit('codex', until, reported=reported)


def _rollout_fallback(cfg, rows: list[tuple[str, str]]) -> dict | None:
    """Search each agent's own isolated CODEX_HOME (they're no longer a shared directory since #62)
    and keep the most recently observed sample across all of them."""
    from .runners import codex_home_dir
    latest = None
    for agent_id, session_id in rows:
        sample = rollout_usage([session_id], codex_home_dir(cfg, agent_id))
        if sample and (latest is None or sample['observed_at'] > latest['observed_at']):
            latest = sample
    return latest


async def monitor(engine) -> None:
    from .runners import codex_home_dir, ensure_codex_home
    while not engine._stop.is_set():
        cfg = engine.cfg
        codex_agents = [a for a in cfg.agents if a.backend == 'codex' or any(p.provider == 'codex' for p in a.providers)]
        if codex_agents:
            # Any codex agent's isolated home carries the same account/auth (symlinked) and the same
            # REQ-BE-015 allowlist config — ensure_codex_home is idempotent, so this is just a cheap
            # guarantee the safe config exists before the RPC runs under it.
            probe = codex_agents[0]
            home = await asyncio.to_thread(ensure_codex_home, cfg, probe, codex_home_dir(cfg, probe.id))
            sample = await app_server_usage(cfg.backends.codex_command, cfg.root, home)
            if not sample:
                rows = [(a['id'], a['session_id']) for a in engine.store.agents()
                        if a['backend'] == 'codex' and a['session_id']]
                sample = await asyncio.to_thread(_rollout_fallback, cfg, rows)
            apply_usage(engine.store, sample, engine.record_limit)
        await asyncio.sleep(REFRESH_SECONDS)
