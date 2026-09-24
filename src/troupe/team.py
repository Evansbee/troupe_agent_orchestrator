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
from .store import ALL_STATUSES, OPEN_STATUSES, Store, HandleBook

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


def fmt_task_line(t: dict, names: HandleBook | None = None) -> str:
    who = (names.name(t["assignee"]) if names else t["assignee"]) if t["assignee"] else f"({t['role']})"
    deps = f" deps:{','.join('#' + str(d) for d in t['depends_on'])}" if t["depends_on"] else ""
    return f"#{t['id']} [{t['status']}] {PRIORITY_NAMES.get(t['priority'], t['priority'])} {t['title']} — {who}{deps}"


def fmt_task_full(store: Store, t: dict, names: HandleBook | None = None) -> str:
    name = names.name if names else str
    lines = [fmt_task_line(t, names), f"created by {name(t['created_by'])} {ago(t['created'])}, updated {ago(t['updated'])}"]
    if t["description"]:
        lines += ["", "Description:", t["description"]]
    if t["acceptance"]:
        lines += ["", "Acceptance criteria:", t["acceptance"]]
    if t["territory"]:
        lines += ["", f"Territory: {t['territory']}"]
    if t["branch"]:
        lines += [f"Branch: {t['branch']}   Worktree: {t['worktree']}"]
    if t["reviewer"]:
        lines += [f"Reviewer: {name(t['reviewer'])}"]
    if t["result"]:
        lines += ["", "Builder's summary:", t["result"]]
    if t["review_notes"]:
        lines += ["", "Latest review notes:", t["review_notes"]]
    notes = store.task_notes(t["id"])
    if notes:
        lines += ["", "Notes:"] + [f"- {name(n['agent'])} ({ago(n['ts'])}): {n['text']}" for n in notes[-12:]]
    return "\n".join(lines)


def fmt_message(m: dict, names: HandleBook | None = None) -> str:
    name = names.name if names else str
    head = f"[msg #{m['id']}] from {name(m['sender'])} → {name(m['recipient'])} ({ago(m['ts'])})"
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
        self.names = HandleBook(cfg.project, cfg.agents)
        self.store = store
        self.me = agent_id
        me = cfg.agent(agent_id)
        self.role = me.role if me else "human"

    def tools(self) -> list:
        return [self.send_message, self.check_inbox, self.ask_human, self.resolve_question, self.propose_idea, self.create_task,
                self.update_task, self.list_tasks, self.get_task, self.complete_task, self.review_task,
                self.remember, self.recall, self.set_status, self.team]

    # ── helpers ───────────────────────────────────────────────────────────
    def _resolve(self, to: str) -> list[str]:
        return self.names.resolve(to, exclude=self.me)

    def _assignment(self, address: str) -> tuple[str | None, str | None]:
        key = address.strip().lstrip("@").casefold()
        if key in self.names.roles and key not in self.names.aliases:
            return None, key  # let the scheduler choose a seat of that role
        ids = self.names.resolve(address)
        if len(ids) != 1 or ids[0] == "human":
            raise ValueError("Choose one agent handle or a role for assignee: " + ", ".join(self.names.handles.values()))
        return ids[0], None

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
        return f"Sent to {', '.join(self.names.name(r) for r in recipients)} (msg {', '.join('#' + str(i) for i in ids)})."

    def check_inbox(self) -> str:
        """Fetch any unread messages that arrived since your wake-up (they're marked read)."""
        msgs = self.store.unread(self.me)
        self.store.mark_read([m["id"] for m in msgs])
        if not msgs:
            return "No new messages."
        return "\n\n".join(fmt_message(m, self.names) for m in msgs)

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

    def resolve_question(self, question_id: int, answer: str, via: str = "chat") -> str:
        """Close an open question after the human answers in chat; then remember the decision.

        Quote the human's answer faithfully. Only the asker, lead or PM can resolve a question.
        `via` is chat or inbox. This records the answer without sending duplicate answer mail."""
        if via not in ("chat", "inbox"):
            return "ERROR: via must be 'chat' or 'inbox'."
        if not answer.strip():
            return "ERROR: answer is empty. Supply the human's actual answer."
        question = self.store.one("SELECT * FROM questions WHERE id=?", question_id)
        if not question:
            return "ERROR: question not found. Check its id in your open questions."
        if question["asker"] != self.me and self.role not in ("lead", "pm"):
            return "ERROR: this is another agent's question. Ask its owner, the lead or PM to resolve it."
        if question["status"] != "open":
            return "ERROR: question is already closed. Use recall to read the recorded answer; do not overwrite it."
        if not self.store.answer(question_id, answer, via=via, notify=False):
            return "ERROR: question was already closed. Use recall to check the recorded answer."
        return f"Question #{question_id} answered via {via}. Record the decision with remember()."

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
        if assignee:
            try:
                assignee, assigned_role = self._assignment(assignee)
                role = assigned_role or role
            except ValueError as e:
                return f"ERROR: {e}"
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
        if edits.get("assignee"):
            try:
                edits["assignee"], assigned_role = self._assignment(edits["assignee"])
                if assigned_role:
                    edits["role"] = assigned_role
            except ValueError as e:
                return f"ERROR: {e}"
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
            try:
                ids = [self.me] if assignee.casefold() == "me" else self.names.resolve(assignee)
            except ValueError as e:
                return f"ERROR: {e}"
            rows = [t for t in rows if t["assignee"] in ids]
        if not rows:
            return "No matching tasks."
        return "\n".join(fmt_task_line(t, self.names) for t in rows)

    def get_task(self, task_id: int) -> str:
        """Full details of a task: brief, acceptance criteria, notes, review history."""
        t = self.store.task(task_id)
        return fmt_task_full(self.store, t, self.names) if t else f"ERROR: no task #{task_id}"

    def complete_task(self, task_id: int, summary: str) -> str:
        """Mark your task finished. `summary`: what changed, how you verified it, anything left undone.

        For code tasks (with a branch) this commits your worktree and sends the task to QA review.
        Other tasks are marked done."""
        t = self.store.task(task_id)
        if not t:
            return f"ERROR: no task #{task_id}"
        if t["assignee"] != self.me and not self._is_lead():
            return f"ERROR: task #{task_id} is assigned to {self.names.name(t['assignee'])}, not you."
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
        return f"Rejected #{task_id}; sent back to {self.names.name(t['assignee'])} with your notes."

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
        if agent:
            try:
                ids = self.names.resolve(agent)
                if len(ids) != 1:
                    return "ERROR: choose one agent handle: " + ", ".join(self.names.handles.values())
                agent = ids[0]
            except ValueError as e:
                return f"ERROR: {e}"
        rows = self.store.memories(agent=agent or None, query=query, kind=kind, limit=limit,
                                   include_private_of=self.me)
        answered = []
        if query:
            answered = [q for q in self.store.questions(status="answered", limit=100)
                        if all(w.lower() in (q["question"] + " " + (q["answer"] or "")).lower()
                               for w in query.split())][:8]
        if not rows and not answered:
            return "Nothing found."
        out = [f"[{m['kind']} #{m['id']}] {m['title']} — {self.names.name(m['agent'])}, {ago(m['ts'])}"
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
            out.append(f"- {self.names.name(a['id'])} — {a['name']}, {role.title} [{a['backend']}{'/' + a['model'] if a['model'] else ''}]"
                       f" · {a['state']}{' · ' + a['status'] if a['status'] else ''}"
                       f"{' · tasks: ' + ' '.join(mine) if mine else ''}")
        return "\n".join(out)
