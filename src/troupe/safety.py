"""Safety policy, redaction, command guards and human stop controls."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import signal
import sys
import threading
import time

PROTECTED = ['src/troupe/roles.py', 'src/troupe/runners.py', 'src/troupe/safety.py',
             'src/troupe/gates.py', 'src/troupe/sandbox/', 'specs/05-safety.md', 'specs/00-vision.md']
PATTERNS = [re.compile(p) for p in (
    r'-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----[\s\S]*?(?:-----END [^-]*PRIVATE KEY-----|$)',
    r'\b(?:AKIA|ASIA)[A-Z0-9]{16}\b',
    r'\b(?:gh[pousr]_|github_pat_|sk-(?:ant-|proj-)?)[A-Za-z0-9_-]{20,}',
    r'(?i)\b(?:password|api[_-]?key|access[_-]?token)\s*[=:]\s*[\"\']?[A-Za-z0-9_+/-]{24,}',
)]


def fingerprint(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def secrets(text: str) -> list[str]:
    return [m.group() for rx in PATTERNS for m in rx.finditer(text)]


def redact(text: str) -> str:
    for rx in PATTERNS:
        text = rx.sub('***', text)
    return text


def audit(store, text: str, *, notify: bool = True) -> None:
    text = redact(text)
    store.event('system', 'safety', text)
    path = Path(store.path).parent / 'engine.log'
    with path.open('a') as log:
        log.write(f'{time.strftime("%Y-%m-%d %H:%M:%S")} safety: {text}\n')
    if notify:
        store.send('system', 'human', text, subject='Safety needs attention', kind='needs_help')


def parse_settings(raw: dict, root: Path) -> dict:
    from .gitops import git
    from .sandbox import parse_role_profiles
    settings = dict(raw)
    settings.setdefault('protected', list(PROTECTED))
    if 'remotes' not in settings:
        settings['remotes'] = git(root, 'remote', check=False).splitlines()
        for name in list(settings['remotes']):
            settings['remotes'] += git(root, 'remote', 'get-url', '--push', '--all', name, check=False).splitlines()
    settings.setdefault('secret_allow', [])
    for key in ('protected', 'remotes', 'secret_allow'):
        if not isinstance(settings[key], list) or not all(isinstance(x, str) for x in settings[key]):
            raise ValueError(f'safety.{key} must be a list of strings')
    settings['roles'] = parse_role_profiles(settings.get('roles', {}))
    return settings


def protected(path: str, settings: dict) -> bool:
    return any(path == p.rstrip('/') or path.startswith(p.rstrip('/') + '/') for p in settings['protected'])


def secret_path(path: str, cwd: Path) -> bool:
    p = Path(os.path.expandvars(os.path.expanduser(path)))
    p = (cwd / p).resolve()
    home = Path.home()
    ssh = home / '.ssh'
    if p.is_relative_to(ssh):
        return not (p.name.endswith('.pub') or p.name == 'known_hosts')
    return p in [home / x for x in ('.aws/credentials', '.config/gh/hosts.yml', '.netrc', '.docker/config.json')]


def state_path(path: str, cwd: Path) -> bool:
    """True if `path` resolves into a `.troupe/` dir's troupe.db (incl. -wal/-shm) or api.sock —
    same files the Bash-command regex below blocks, but reached via a file-editing tool's own path
    argument instead of a shell command (e.g. Claude's Write/Edit tools, not just Bash)."""
    if not path:
        return False
    p = Path(os.path.expandvars(os.path.expanduser(path)))
    p = (cwd / p).resolve()
    return p.parent.name == '.troupe' and (p.name == 'api.sock' or p.name.startswith('troupe.db'))


def guard(tool: str, args: dict, cwd: Path, settings: dict) -> str | None:
    """Conservative command inspection, not an OS sandbox (see safety ADR)."""
    raw = json.dumps(args)
    if secrets(raw):
        return 'Secret material in tool arguments'
    if tool in ('Read', 'Edit', 'Write', 'read_file', 'write_file'):
        if secret_path(args.get('file_path', args.get('path', '')), cwd):
            return 'Reading or modifying a secret store'
    if tool in ('Read', 'Edit', 'Write', 'MultiEdit', 'NotebookEdit', 'read_file', 'write_file'):
        # The Bash regex below blocks cat/sqlite3/nc/curl access to these same files; a file-editing
        # tool's own path argument is an equally direct route and must be blocked the same way,
        # for both reads (leaking kv contents) and writes (clobbering the DB or socket).
        path = args.get('file_path', args.get('notebook_path', args.get('path', '')))
        if state_path(path, cwd):
            return "Direct access to troupe's database or local API socket; use the provided tools"
    if tool != 'Bash':
        return None
    command = args.get('command', '')
    try:
        words = shlex.split(command.replace('\n', ' ; '))
    except ValueError:
        return 'Unparseable shell command'
    if re.search(r'\$\{?[A-Z_]*(?:TOKEN|PASSWORD|SECRET|API_KEY)[A-Z_]*', command):
        return 'Expanding secret environment variables into tool output or requests'
    if re.search(r'\bsecurity\s+find-\S*password\b|\bprintenv\b|\benv(?:\s+-[0u]+)*\s*(?:$|[|;>])', command):
        return 'Dumping credentials or environment secrets'
    if re.search(r'\btroupe\.db\b|\bapi\.sock\b', command):
        # Direct DB/socket access would bypass the MCP tool layer entirely (an agent could write
        # kv or questions, or reach the human-only API, without going through any of the guards or
        # review gates those tools enforce). Legitimate access is always through the MCP server's
        # own process, never a Bash command the agent runs itself.
        return "Direct access to troupe's database or local API socket; use the provided tools"
    if any(secret_path(w, cwd) for w in words if '/' in w):
        # Authentication consumes keys without putting them in the model context.
        if not (words and Path(words[0]).name == 'ssh' and all(i > 0 and words[i-1] == '-i' for i, w in enumerate(words) if '/' in w and secret_path(w, cwd)) and not re.search(r'[;|&><`]|\$\(', command)):
            return 'Reading a secret store into agent context'
    for i, word in enumerate(words):
        if Path(word).name != 'git':
            continue
        tail = words[i + 1:]
        if 'remote' in tail and any(x in tail for x in ('add', 'set-url', 'rename', 'remove', 'rm')):
            return 'Changing project remotes'
        if 'config' in tail and any('remote.' in x or 'alias.' in x for x in tail):
            return 'Changing remote or command alias configuration'
        if 'push' not in tail:
            continue
        if any(x in ('-C', '-c') or x.startswith(('--git-dir', '--work-tree', '--config-env')) for x in tail):
            return 'Push repository/config overrides require human review'
        tail = tail[tail.index('push') + 1:]
        if any(x == '--repo' or x.startswith('--repo=') for x in tail):
            return 'Push overrides require human review'
        positional = [x for x in tail if not x.startswith('-') and x not in (';', '&&', '||')]
        remote = positional[0] if positional else 'origin'
        if remote not in settings['remotes']:
            return 'Push destination is not an approved project remote'
        from .gitops import git
        default = git(cwd, 'symbolic-ref', '--short', 'refs/remotes/origin/HEAD', check=False).removeprefix('origin/')
        if not default:
            common = Path(git(cwd, 'rev-parse', '--git-common-dir', check=False))
            root = (cwd / common).resolve().parent
            default = git(root, 'symbolic-ref', '--short', 'HEAD', check=False)
        if any(x == '--mirror' or x.startswith('--force') or re.match(r'^-[^-]*f', x) or x.startswith('+') for x in tail):
            # Without an explicit safe destination, force can affect the default branch.
            refs = positional[1:]
            if not refs or any('*' in x or '?' in x or '[' in x or x.lstrip('+').split(':')[-1].removeprefix('refs/heads/') in ('main', 'master', 'HEAD', default) for x in refs):
                return 'Force push to the default branch'
    return None


def stop_now(store) -> None:
    store.kv_set('stopped', True)
    store.kv_set('paused', True)
    store.command('stop_now')
    audit(store, 'Stop everything requested by the human')


def resume(store) -> None:
    if os.environ.get('TROUPE_AGENT'):
        raise PermissionError('Only the human may resume; use ask_human')
    store.command('resume')


def kill_group(pid: int) -> None:
    try:
        os.killpg(pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        return
    def finish():
        try:
            os.killpg(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
    timer = threading.Timer(0.35, finish)
    timer.daemon = True
    timer.start()


def scan_staged(cwd: Path) -> None:
    from .gitops import git, GitError
    from .store import Store
    common = Path(git(cwd, 'rev-parse', '--git-common-dir'))
    root = (cwd / common).resolve().parent
    state = root / '.troupe'
    settings = {}
    store = Store(state / 'troupe.db') if state.exists() else None
    if store:
        settings = store.kv_get('safety.approved', {}).get('safety', {})
    diff = git(cwd, 'diff', '--cached', '--no-ext-diff', '--unified=0')
    added = '\n'.join(line[1:] for line in diff.splitlines() if line.startswith('+') and not line.startswith('+++'))
    hits = [fingerprint(hit) for hit in secrets(added) if fingerprint(hit) not in settings.get('secret_allow', [])]
    if hits:
        message = 'Commit blocked: secret pattern detected (***); use ask_human. Fingerprints: ' + ', '.join(hits)
        if store:
            audit(store, message)
        raise GitError(message)


def hook_main() -> None:
    from .config import load_runtime
    from .store import Store
    try:
        payload = json.load(sys.stdin)
        cfg = load_runtime(Path(os.environ['TROUPE_ROOT']))
        reason = guard(payload.get('tool_name', ''), payload.get('tool_input', {}),
                       Path(payload.get('cwd', cfg.root)), cfg.safety)
        if reason:
            audit(Store(cfg.db_path), f'Blocked {payload.get("tool_name")}: {reason}')
            print(json.dumps({'hookSpecificOutput': {'hookEventName': 'PreToolUse',
                'permissionDecision': 'deny', 'permissionDecisionReason': f'ERROR: {reason}. Use ask_human.'}}))
    except Exception:
        print('ERROR: safety hook unavailable; use ask_human.', file=sys.stderr)
        sys.exit(2)


if __name__ == '__main__':
    hook_main()
