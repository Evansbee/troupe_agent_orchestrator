"""Spend accounting (REQ-ENG-070): cost, tokens and rework across runs.

Read-only over runs/tasks/events -- no new tracking. Two things are *derived*, not stored:
- "produced nothing": a run with zero tool-call run_lines (kind='tool'). Every meaningful action in
  troupe (an edit, a commit, a message, an MCP call) happens through a tool call, so this is a
  sufficient proxy for "no tool calls, no commit, no message" without needing to separately
  correlate git history or the messages table.
- "rework": a run on a task after that task was sent back -- a QA rejection (team.py's
  review_task), a merge-gate bounce (gates.py's check_failed), or a merge conflict (gates.py);
  all three log a `kind='task', ref='task:<id>'` event with recognizable wording, checked by
  timestamp against the run's own start. A "messages"-reason run that produced nothing is also
  rework: the wake fired, but there was nothing left to act on by the time it ran (a duplicate
  delivery of a trigger someone else already handled).
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass

SINCE_SECONDS = {"24h": 86400.0, "7d": 7 * 86400.0}
GROUP_BYS = ("task", "agent", "reason", "day")

# gates.py/team.py's exact wording for the three "sent back" events (see module docstring).
_REWORK_EVENT = re.compile(r" rejected #\d+|Checks failed on #\d+|Merge conflict on #\d+")


def since_cutoff(since: str) -> float | None:
    if since == "all":
        return None
    if since not in SINCE_SECONDS:
        raise ValueError(f"--since must be one of 24h, 7d, all (got {since!r})")
    return time.time() - SINCE_SECONDS[since]


@dataclass
class RunRow:
    id: int
    agent: str
    reason: str
    status: str
    cost: float
    tokens: int
    task_id: int | None
    started: float
    ended: float
    chat: bool
    produced_nothing: bool
    rework: bool

    @property
    def wall_seconds(self) -> float:
        return max(0.0, self.ended - self.started)


def _tool_call_run_ids(store, run_ids: list[int]) -> set[int]:
    if not run_ids:
        return set()
    placeholders = ",".join("?" * len(run_ids))
    rows = store.q(f"SELECT DISTINCT run_id FROM run_lines WHERE kind='tool' AND run_id IN ({placeholders})",
                  *run_ids)
    return {r["run_id"] for r in rows}


def _rework_trigger_timestamps(store) -> dict[int, list[float]]:
    """task_id -> sorted timestamps of its QA-reject / gate-bounce / merge-conflict events. A run
    on that task starting after any of these is rework."""
    rows = store.q("SELECT ref, ts, text FROM events WHERE kind='task' AND ref LIKE 'task:%'")
    out: dict[int, list[float]] = {}
    for r in rows:
        if not _REWORK_EVENT.search(r["text"] or ""):
            continue
        try:
            tid = int(r["ref"].split(":", 1)[1])
        except (IndexError, ValueError):
            continue
        out.setdefault(tid, []).append(r["ts"])
    for tid in out:
        out[tid].sort()
    return out


def load_runs(store, since: str = "all") -> list[RunRow]:
    """Every *finished* run (ended is not NULL) at or after the cutoff, with produced_nothing and
    rework already computed."""
    cutoff = since_cutoff(since)
    if cutoff is None:
        rows = store.q("SELECT * FROM runs WHERE ended IS NOT NULL")
    else:
        rows = store.q("SELECT * FROM runs WHERE ended IS NOT NULL AND started>=?", cutoff)
    tool_ids = _tool_call_run_ids(store, [r["id"] for r in rows])
    triggers = _rework_trigger_timestamps(store)
    out = []
    for r in rows:
        produced_nothing = r["id"] not in tool_ids
        rework = False
        if r["reason"] == "task" and r["task_id"] is not None:
            rework = any(ts < r["started"] for ts in triggers.get(r["task_id"], []))
        elif r["reason"] == "messages" and produced_nothing:
            rework = True
        out.append(RunRow(
            id=r["id"], agent=r["agent"], reason=r["reason"], status=r["status"] or "",
            cost=r["cost"] or 0.0, tokens=r["tokens"] or 0, task_id=r["task_id"],
            started=r["started"], ended=r["ended"], chat=bool(r["chat"]),
            produced_nothing=produced_nothing, rework=rework,
        ))
    return out


def _totals(runs: list[RunRow]) -> dict:
    rework = [r for r in runs if r.rework]
    return dict(
        runs=len(runs), cost=sum(r.cost for r in runs), tokens=sum(r.tokens for r in runs),
        wall_seconds=sum(r.wall_seconds for r in runs),
        rework_runs=len(rework), rework_cost=sum(r.cost for r in rework),
        rework_share=(len(rework) / len(runs)) if runs else 0.0,
    )


def by_agent(store, runs: list[RunRow]) -> list[dict]:
    agents = {r.agent for r in runs}
    rows = []
    for agent in sorted(agents):
        mine = [r for r in runs if r.agent == agent]
        totals = _totals(mine)
        nothing = [r for r in mine if r.produced_nothing]
        rows.append(dict(
            agent=agent, **totals,
            avg_cost=(totals["cost"] / totals["runs"]) if totals["runs"] else 0.0,
            produced_nothing=len(nothing),
            produced_nothing_share=(len(nothing) / len(mine)) if mine else 0.0,
        ))
    rows.sort(key=lambda r: -r["cost"])
    return rows


def by_reason(store, runs: list[RunRow]) -> list[dict]:
    reasons = {r.reason for r in runs}
    rows = [dict(reason=reason, **_totals([r for r in runs if r.reason == reason]))
           for reason in sorted(reasons)]
    rows.sort(key=lambda r: -r["cost"])
    return rows


def by_day(store, runs: list[RunRow]) -> list[dict]:
    days = {time.strftime("%Y-%m-%d", time.localtime(r.started)) for r in runs}
    rows = [dict(day=day, **_totals([r for r in runs
                                    if time.strftime("%Y-%m-%d", time.localtime(r.started)) == day]))
           for day in sorted(days)]
    return rows


def by_task(store, runs: list[RunRow]) -> list[dict]:
    """Per task: cost/runs/bounces plus the cost specifically of QA review runs on it. A "bounce"
    is a QA-reject/gate-bounce/merge-conflict event on the task (not a run) -- gate re-checks
    themselves are plain `git.check` subprocesses with no agent or cost attached, so their count is
    the only thing attributable here, not a dollar figure."""
    triggers = _rework_trigger_timestamps(store)
    task_ids = {r.task_id for r in runs if r.task_id is not None}
    rows = []
    for tid in task_ids:
        mine = [r for r in runs if r.task_id == tid]
        totals = _totals(mine)
        task = store.task(tid)
        review_cost = sum(r.cost for r in mine if r.reason == "review")
        rows.append(dict(
            task_id=tid, title=(task["title"] if task else "(deleted)"),
            status=(task["status"] if task else "?"), **totals,
            bounces=len(triggers.get(tid, [])), review_cost=review_cost,
        ))
    rows.sort(key=lambda r: -r["cost"])
    return rows


def summary(store, runs: list[RunRow]) -> dict:
    return _totals(runs)


REPORTERS = {"task": by_task, "agent": by_agent, "reason": by_reason, "day": by_day}

DISCLAIMER = ("USD at API list rates as reported by the backend; on a subscription your bill is "
             "the subscription, the tokens still count toward your caps.")


def format_report(total: dict, since: str, by: str, rows: list[dict], name=str) -> str:
    """Shared by `troupe spend`, the TUI's spend screen and the GUI's Pulse tab, so all three read
    the same numbers the same way. `name` resolves an agent id to a display handle."""
    lines = [
        f"{since}: {total['runs']} runs, ${total['cost']:.2f}, {total['tokens']:,} tokens, "
        f"{total['wall_seconds'] / 60:.0f} min wall time, rework {total['rework_runs']}/{total['runs']} "
        f"({total['rework_share'] * 100:.0f}%, ${total['rework_cost']:.2f})",
        DISCLAIMER,
    ]
    if not rows:
        lines.append(f"\nno runs {'' if since == 'all' else 'in the last ' + since}")
        return "\n".join(lines)
    if by == "task":
        lines.append(f"\n{'task':<7} {'status':<12} {'runs':>5} {'bounces':>8} {'cost':>9} {'review $':>9}  title")
        for r in rows[:10]:
            lines.append(f"#{r['task_id']:<6} {r['status']:<12} {r['runs']:>5} {r['bounces']:>8} "
                         f"${r['cost']:>8.2f} ${r['review_cost']:>8.2f}  {r['title'][:50]}")
    elif by == "agent":
        lines.append(f"\n{'agent':<14} {'runs':>5} {'cost':>9} {'avg $':>7} {'tokens':>10} {'rework%':>8} {'nothing%':>9}")
        for r in rows:
            lines.append(f"{name(r['agent']):<14} {r['runs']:>5} ${r['cost']:>8.2f} ${r['avg_cost']:>6.2f} "
                         f"{r['tokens']:>10,} {r['rework_share'] * 100:>7.0f}% {r['produced_nothing_share'] * 100:>8.0f}%")
    elif by == "reason":
        lines.append(f"\n{'reason':<10} {'runs':>5} {'cost':>9} {'tokens':>10} {'rework%':>8}")
        for r in rows:
            lines.append(f"{r['reason']:<10} {r['runs']:>5} ${r['cost']:>8.2f} {r['tokens']:>10,} {r['rework_share'] * 100:>7.0f}%")
    else:
        lines.append(f"\n{'day':<12} {'runs':>5} {'cost':>9} {'tokens':>10} {'rework%':>8}")
        for r in rows:
            lines.append(f"{r['day']:<12} {r['runs']:>5} ${r['cost']:>8.2f} {r['tokens']:>10,} {r['rework_share'] * 100:>7.0f}%")
    return "\n".join(lines)
