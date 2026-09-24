"""The troupe tool API. Exposed to claude/codex via MCP and to local models via a native tool loop.

Every public method here becomes an agent tool: name = method name, description = docstring,
schema = signature. Methods return short plain-text results written for an LLM to read.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Literal

from . import gitops
from .config import Config
from .roles import get_role
from .store import ALL_STATUSES, OPEN_STATUSES, Store

MAX_OPEN_QUESTIONS_PER_AGENT = 4
PRIORITY_NAMES = {0: "P0-urgent", 1: "P1-high", 2: "P2-normal", 3: "P3-low"}


def ago(ts: float | None) -> str:
    if not ts:
        return "never"
    d = time.time() - ts
    if d < 60:
        return f"{int(d)}s ago"
    if d < 3600:
        return f"{int(d // 60)}m ago"
    if d < 86400:
        return f"{int(d // 3600)}h ago"
    return f"{int(d // 86400)}d ago"


def fmt_task_line(t: dict) -> str:
    who = t["assignee"] or f"({t['role']})"
    deps = f" deps:{','.join('#' + str(d) for d in t['depends_on'])}" if t["depends_on"] else ""
    return f"#{t['id']} [{t['status']}] {PRIORITY_NAMES.get(t['priority'], t['priority'])} {t['title']} — {who}{deps}"


def fmt_task_full(store: Store, t: dict) -> str:
    lines = [fmt_task_line(t), f"created by {t['created_by']} {ago(t['created'])}, updated {ago(t['updated'])}"]
    if t["description"]:
        lines += ["", "Description:", t["description"]]
    if t["acceptance"]:
        lines += ["", "Acceptance criteria:", t["acceptance"]]
    if t["territory"]:
        lines += ["", f"Territory: {t['territory']}"]
    if t["branch"]:
        lines += [f"Branch: {t['branch']}   Worktree: {t['worktree']}"]
    if t["reviewer"]:
        lines += [f"Reviewer: {t['reviewer']}"]
    if t["result"]:
        lines += ["", "Builder's summary:", t["result"]]
    if t["review_notes"]:
        lines += ["", "Latest review notes:", t["review_notes"]]
    notes = store.task_notes(t["id"])
    if notes:
        lines += ["", "Notes:"] + [f"- {n['agent']} ({ago(n['ts'])}): {n['text']}" for n in notes[-12:]]
    return "\n".join(lines)


def fmt_message(m: dict) -> str:
    head = f"[msg #{m['id']}] from {m['sender']} → {m['recipient']} ({ago(m['ts'])})"
    if m["subject"]:
        head += f" — {m['subject']}"
    if m["reply_to"]:
        head += f"  (re #{m['reply_to']})"
    if m["task_id"]:
        head += f"  [task #{m['task_id']}]"
    return f"{head}\n{m['body'].strip()}"


class TeamAPI:
    def __init__(self, cfg: Config, store: Store, agent_id: str):
        self.cfg = cfg
        self.store = store
        self.me = agent_id
        me = cfg.agent(agent_id)
        self.role = me.role if me else "human"

    def tools(self) -> list:
        return [self.send_message, self.check_inbox, self.ask_human, self.propose_idea, self.create_task,
                self.update_task, self.list_tasks, self.get_task, self.complete_task, self.review_task,
                self.remember, self.recall, self.set_status, self.team]

    # ── helpers ───────────────────────────────────────────────────────────
    def _resolve(self, to: str) -> list[str]:
        to = to.strip().lstrip("@")
        if to in ("human", "user", "owner"):
            return ["human"]
        if to in ("team", "all", "everyone"):
            return [a.id for a in self.cfg.agents if a.id != self.me]
        if self.cfg.agent(to):
            return [to]
        by_role = [a.id for a in self.cfg.agents_with_role(to) if a.id != self.me]
        if by_role:
            return by_role
        raise ValueError(f"unknown recipient {to!r}. Use an agent id ({', '.join(a.id for a in self.cfg.agents)}),"
                         " a role, 'team', or 'human'.")

    def _is_lead(self) -> bool:
        return self.role == "lead" or self.me == "human"

    # ── mail ──────────────────────────────────────────────────────────────
    def send_message(self, to: str, body: str, subject: str = "", reply_to: int | None = None,
                     task_id: int | None = None) -> str:
        """Send a message to a teammate's mailbox; it wakes them up.

        `to`: an agent id (e.g. "lead", "builder-1"), a role (e.g. "builder" = every builder), "team"
        (everyone), or "human" (the project owner — for decisions prefer ask_human). Keep it concise and
        specific, one topic per message. Reference tasks (#12), specs (specs/10-auth.md REQ-AUTH-004) and
        message ids (reply_to) so the recipient has full context."""
        try:
            recipients = self._resolve(to)
        except ValueError as e:
            return f"ERROR: {e}"
        ids = [self.store.send(self.me, r, body, subject=subject, reply_to=reply_to, task_id=task_id)
               for r in recipients]
        return f"Sent to {', '.join(recipients)} (msg {', '.join('#' + str(i) for i in ids)})."

    def check_inbox(self) -> str:
        """Fetch any unread messages that arrived since your wake-up (they're marked read)."""
        msgs = self.store.unread(self.me)
        self.store.mark_read([m["id"] for m in msgs])
        if not msgs:
            return "No new messages."
        return "\n\n".join(fmt_message(m) for m in msgs)

    # ── the human ─────────────────────────────────────────────────────────
    def ask_human(self, question: str, context: str = "", options: list[str] | None = None,
                  task_id: int | None = None) -> str:
        """Ask the project owner a question. It appears in their "Needs you" inbox; the answer arrives
        later in your mailbox — do NOT wait for it, continue with other work.

        Offer 2-5 concrete `options` whenever possible (they can still reply free-form). Put what
        prompted the question in `context` (quote the spec line / task). Check recall() first — never
        re-ask something already answered."""
        open_q = self.store.scalar("SELECT COUNT(*) FROM questions WHERE asker=? AND status='open'", self.me,
                                   default=0)
        if open_q >= MAX_OPEN_QUESTIONS_PER_AGENT:
            return (f"ERROR: you already have {open_q} unanswered questions waiting on the human. "
                    "Wait for answers before asking more; prioritize.")
        qid = self.store.ask(self.me, question, context, options, task_id=task_id)
        return f"Question #{qid} is in the human's inbox. The answer will arrive in your mailbox."

    def propose_idea(self, title: str, pitch: str, why: str = "") -> str:
        """Propose a product idea to the human (they answer: yes / no / later / sort of).

        Use for improvements beyond the current spec: features, polish, things comparable products do.
        `pitch`: what it is and what it looks like for the user. `why`: the evidence and the value."""
        context = pitch + (f"\n\nWhy: {why}" if why else "")
        qid = self.store.ask(self.me, title, context, ["Yes, do it", "No", "Later", "Sort of — let's discuss"],
                             kind="idea")
        return f"Idea #{qid} sent to the human. Their answer will arrive in your mailbox."

    # ── tasks ─────────────────────────────────────────────────────────────
    def create_task(self, title: str, description: str, acceptance: str = "", territory: str = "",
                    role: str = "builder", assignee: str | None = None, priority: int = 2,
                    depends_on: list[int] | None = None) -> str:
        """Create a task. Write it as a complete brief a teammate can execute without asking:
        `description` (goal + context paths: spec sections, files), `acceptance` (testable criteria),
        `territory` (files/dirs it owns, e.g. "src/auth/, tests/test_auth.py").

        `role`: which role does it ("builder", "designer", "spec", "qa"...). `assignee`: a specific agent
        id, or leave empty to let the orchestrator dispatch to a free agent of that role.
        `priority`: 0 urgent, 1 high, 2 normal, 3 low. `depends_on`: task ids that must be done first.
        Tasks created by the lead are ready immediately; others land in backlog for the lead to triage."""
        try:
            get_role(role)
        except KeyError as e:
            return f"ERROR: {e}"
        if assignee and not self.cfg.agent(assignee):
            return f"ERROR: unknown assignee {assignee!r}"
        status = "ready" if self._is_lead() else "backlog"
        tid = self.store.add_task(title, description, acceptance, territory, status=status,
                                  priority=max(0, min(3, priority)), role=role, assignee=assignee or None,
                                  created_by=self.me, depends_on=depends_on)
        return f"Created task #{tid} [{status}]."

    def update_task(self, task_id: int, status: str | None = None, note: str = "", priority: int | None = None,
                    assignee: str | None = None, title: str | None = None, description: str | None = None,
                    acceptance: str | None = None, territory: str | None = None,
                    depends_on: list[int] | None = None) -> str:
        """Update a task and/or add a note to it.

        Statuses: backlog, ready, in_progress, blocked, review, done, cancelled. The lead can change
        anything. Assignees can set their own task to "blocked" (say why in `note`) or back to
        "in_progress". Anyone can add a `note`. To finish work use complete_task instead."""
        t = self.store.task(task_id)
        if not t:
            return f"ERROR: no task #{task_id}"
        fields: dict = {}
        mine = t["assignee"] == self.me
        if status:
            if status not in ALL_STATUSES:
                return f"ERROR: unknown status {status!r}; use one of {', '.join(ALL_STATUSES)}"
            if not self._is_lead() and not (mine and status in ("blocked", "in_progress")):
                return "ERROR: only the lead can move tasks to that status. Add a note or message the lead."
            fields["status"] = status
            if status == "in_progress":
                fields["next_attempt_at"] = 0
        edits = {"priority": priority, "assignee": assignee, "title": title, "description": description,
                 "acceptance": acceptance, "territory": territory, "depends_on": depends_on}
        edits = {k: v for k, v in edits.items() if v is not None}
        if edits and not (self._is_lead() or (mine and set(edits) <= {"description", "acceptance", "territory"})):
            return "ERROR: only the lead can re-prioritize/re-assign tasks. Add a note or message the lead."
        if "assignee" in edits and edits["assignee"] and not self.cfg.agent(edits["assignee"]):
            return f"ERROR: unknown assignee {edits['assignee']!r}"
        fields.update(edits)
        if note:
            self.store.task_note(task_id, self.me, note)
        if fields:
            desc = ", ".join(f"{k}={v}" for k, v in fields.items() if k not in ("description", "acceptance"))
            self.store.update_task(task_id, actor=self.me, event_text=f"{self.me} updated #{task_id}: {desc or 'details'}",
                                   **fields)
        elif note:
            self.store.event(self.me, "task", f"{self.me} noted on #{task_id}: {note[:100]}", ref=f"task:{task_id}")
        return f"Task #{task_id} updated."

    def list_tasks(self, status: str = "open", assignee: str = "") -> str:
        """List tasks. `status`: "open" (default: everything not done/cancelled), "all", or one status.
        `assignee`: filter by agent id ("me" for yours)."""
        if status == "open":
            rows = self.store.tasks(OPEN_STATUSES)
        elif status == "all":
            rows = self.store.tasks(limit=300)
        else:
            rows = self.store.tasks((status,))
        if assignee:
            who = self.me if assignee == "me" else assignee
            rows = [t for t in rows if t["assignee"] == who]
        if not rows:
            return "No matching tasks."
        return "\n".join(fmt_task_line(t) for t in rows)

    def get_task(self, task_id: int) -> str:
        """Full details of a task: brief, acceptance criteria, notes, review history."""
        t = self.store.task(task_id)
        return fmt_task_full(self.store, t) if t else f"ERROR: no task #{task_id}"

    def complete_task(self, task_id: int, summary: str) -> str:
        """Mark your task finished. `summary`: what changed, how you verified it, anything left undone.

        For code tasks (with a branch) this commits your worktree and sends the task to QA review.
        Other tasks are marked done."""
        t = self.store.task(task_id)
        if not t:
            return f"ERROR: no task #{task_id}"
        if t["assignee"] != self.me and not self._is_lead():
            return f"ERROR: task #{task_id} is assigned to {t['assignee']}, not you."
        if get_role(t["role"]).works_in_task_tree and t["role"] != "qa" and not t["branch"]:
            return ("ERROR: code tasks are built in a task worktree, which this task doesn't have yet. "
                    "Stop here; the orchestrator will wake you inside its worktree.")
        if t["branch"] and t["worktree"]:
            try:
                gitops.commit_all(Path(t["worktree"]), f"#{task_id} {t['title']}\n\n{summary}")
            except gitops.GitError as e:
                return f"ERROR committing worktree: {e}"
            self.store.update_task(task_id, actor=self.me, status="review", result=summary, reviewer=None,
                                   event_text=f"{self.me} submitted #{task_id} for review: {t['title']}")
            return f"Task #{task_id} committed on {t['branch']} and sent to QA review. Nice work."
        self.store.update_task(task_id, actor=self.me, status="done", result=summary,
                               event_text=f"{self.me} completed #{task_id}: {t['title']}")
        return f"Task #{task_id} marked done."

    def review_task(self, task_id: int, verdict: Literal["approve", "reject"], notes: str) -> str:
        """QA verdict on a task in review. "approve" merges the branch into main; "reject" sends it back
        to the builder. `notes`: for rejections, concrete reproducible failures (steps, expected vs actual,
        which acceptance criterion / REQ fails); for approvals, how you verified it."""
        t = self.store.task(task_id)
        if not t:
            return f"ERROR: no task #{task_id}"
        if t["status"] != "review":
            return f"ERROR: task #{task_id} is {t['status']}, not in review."
        if self.role not in ("qa", "lead") and self.me != "human":
            return "ERROR: only QA or the lead can review tasks."
        self.store.task_note(task_id, self.me, f"REVIEW {verdict.upper()}: {notes}")
        if verdict == "approve":
            self.store.update_task(task_id, actor=self.me, status="approved", review_notes=notes,
                                   event_text=f"{self.me} approved #{task_id} {t['title']}")
            return f"Approved #{task_id}. The orchestrator will merge it into main."
        self.store.update_task(task_id, actor=self.me, status="in_progress", review_notes=notes, next_attempt_at=0,
                               event_text=f"{self.me} rejected #{task_id} {t['title']}")
        if t["assignee"]:
            self.store.send(self.me, t["assignee"], notes, subject=f"Review: #{task_id} needs changes",
                            task_id=task_id)
        return f"Rejected #{task_id}; sent back to {t['assignee']} with your notes."

    # ── memory ────────────────────────────────────────────────────────────
    def remember(self, title: str, content: str = "", rationale: str = "",
                 kind: Literal["decision", "note", "fact", "idea", "preference"] = "decision",
                 private: bool = False) -> str:
        """Record something in memory so it outlives this session. Use for every non-trivial decision
        (with its `rationale` — WHY), facts learned, the human's preferences, and parked ideas.
        Team memory is visible to everyone; `private=True` keeps a working note just for you."""
        mid = self.store.remember(self.me, title, content, rationale, kind, "private" if private else "team")
        return f"Remembered ({kind} #{mid})."

    def recall(self, query: str = "", agent: str = "", kind: str = "", limit: int = 15) -> str:
        """Search team memory (plus your private notes) — decisions, facts, preferences, ideas.
        `query`: keywords (all must match). Filter by `agent` or `kind`. Check before deciding or asking."""
        rows = self.store.memories(agent=agent or None, query=query, kind=kind, limit=limit,
                                   include_private_of=self.me)
        answered = []
        if query:
            answered = [q for q in self.store.questions(status="answered", limit=100)
                        if all(w.lower() in (q["question"] + " " + (q["answer"] or "")).lower()
                               for w in query.split())][:8]
        if not rows and not answered:
            return "Nothing found."
        out = [f"[{m['kind']} #{m['id']}] {m['title']} — {m['agent']}, {ago(m['ts'])}"
               + (f"\n  {m['content']}" if m["content"] else "")
               + (f"\n  why: {m['rationale']}" if m["rationale"] else "") for m in rows]
        out += [f"[answered question #{q['id']}] {q['question']} → human: {q['answer']}" for q in answered]
        return "\n".join(out)

    # ── presence ──────────────────────────────────────────────────────────
    def set_status(self, text: str) -> str:
        """Set your one-line status shown to the team and the human (e.g. "drafting specs/20-sync.md")."""
        self.store.set_agent(self.me, status=text[:200])
        self.store.event(self.me, "status", f"{self.me}: {text[:120]}", significant=False)
        return "Status set."

    def team(self) -> str:
        """Who's on the team, what each is doing right now, and their open tasks."""
        out = []
        open_tasks = self.store.tasks(OPEN_STATUSES)
        for a in self.store.agents():
            role = get_role(a["role"])
            mine = [f"#{t['id']}({t['status']})" for t in open_tasks if t["assignee"] == a["id"]]
            out.append(f"- {a['id']} — {a['name']}, {role.title} [{a['backend']}{'/' + a['model'] if a['model'] else ''}]"
                       f" · {a['state']}{' · ' + a['status'] if a['status'] else ''}"
                       f"{' · tasks: ' + ' '.join(mine) if mine else ''}")
        return "\n".join(out)
