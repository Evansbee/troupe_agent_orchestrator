"""TUI slice B (#67): Needs-you pane — open questions, ideas, and safety approval cards.

Talks to the engine only through the API client (REQ-TUI-002): `client.call(method, timeout=5.0,
**params) -> dict`. Built against that duck-typed shape rather than importing `tui.client` (#66,
built in parallel) — swap in the real `TuiClient` once it lands, no changes needed here.
"""
from __future__ import annotations

import json
from typing import Any

from rich.markup import escape
from textual.app import ComposeResult
from textual.binding import Binding
from textual.reactive import reactive
from textual.widget import Widget
from textual.widgets import Input, ListItem, ListView, Static

from . import AutoRetrier, ReloadCoalescer

APPROVAL_KINDS = ("approval", "safety")
CALL_TIMEOUT = 5.0

# #109/design/system.md "Pinning & priority order": exactly REQ-COM-029's must-deliver set that
# can appear as a card — concern outranks safety (Principle 0 over a protected-merge approval),
# which outranks the team's own stability (crash/crash_loop, kept together since neither is senior
# to the other). Only `kind` values the pane can actually receive today are wired in; #97 plugs
# `concern`/`crash`/`crash_loop` into this same list/questions feed later with no ordering change.
# The API rewrites a stored `kind='safety'` row to `'approval'` before the TUI ever sees it
# (api.py's `question()`), so `'approval'` — not `'safety'` — is the literal value pinning has to
# match; both are listed so this stays correct if that ever changes.
_PIN_RANK = {"concern": 0, "safety": 1, "approval": 1, "crash": 2, "crash_loop": 2}


def is_safety(question: dict) -> bool:
    return question.get("kind") in APPROVAL_KINDS


def pin_rank(question: dict) -> int | None:
    """None for an ordinary card (unaffected, newest-first as today); otherwise the pinned group
    it belongs to, lowest first: concern (0), safety (1), crash/crash_loop (2)."""
    return _PIN_RANK.get(question.get("kind"))


def sort_cards(items: list[dict]) -> list[dict]:
    """Pinned kinds float to the top of the list, ordered concern > safety > crash/crash_loop; the
    safety group is oldest-first within itself (these gate a merge — newest-first would let an
    older pending approval rot under a newer one), everything else (including the other pinned
    groups) stays newest-first, matching the API's own delivery order for the un-pinned tail."""
    def key(q: dict) -> tuple[int, float]:
        # Only ever called on an already-pinned item (rank is never None here) — see the generator
        # expression below, which filters before sorting.
        rank = pin_rank(q)
        ts = q.get("ts", 0) or 0
        return (rank, ts) if rank == 1 else (rank, -ts)
    pinned = sorted((q for q in items if pin_rank(q) is not None), key=key)
    rest = [q for q in items if pin_rank(q) is None]
    return pinned + rest


def split_decision(text: str) -> tuple[str, str]:
    """"Reject — too broad" -> ("reject", "too broad"); mirrors gates.py's own parsing."""
    head, _, note = text.partition(" — ")
    return head.strip().lower(), note.strip()


def _diff_named_list(before: list | None, after: list | None) -> str:
    """+/- the actual item names, not counts (QA #85 round 2: a bare "+1 -0" hides *what* changed —
    a new push remote or a path losing protection is exactly the thing the human must see before
    approving)."""
    before_set, after_set = set(before or []), set(after or [])
    added = [f"+{x}" for x in sorted(after_set - before_set)]
    removed = [f"−{x}" for x in sorted(before_set - after_set)]
    return ", ".join(added + removed)


def _diff_roles(previous: dict | None, proposed: dict | None) -> list[str]:
    """Per-role diff: added/removed roles in full, and changed keys within a role shown as
    before -> after — a role quietly gaining e.g. network access must be visible by name."""
    prev_roles, next_roles = previous or {}, proposed or {}
    lines = []
    for role in sorted(set(next_roles) - set(prev_roles)):
        lines.append(f"+{role} {json.dumps(next_roles[role], sort_keys=True)}")
    for role in sorted(set(prev_roles) - set(next_roles)):
        lines.append(f"−{role}")
    for role in sorted(set(prev_roles) & set(next_roles)):
        before, after = prev_roles[role] or {}, next_roles[role] or {}
        if before == after:
            continue
        for key in sorted(set(before) | set(after)):
            bv, av = before.get(key), after.get(key)
            if bv != av:
                lines.append(f"{role}.{key}: {json.dumps(bv)} → {json.dumps(av)}")
    return lines


_KNOWN_SAFETY_FIELDS = ("protected", "remotes", "secret_allow", "roles")
_KNOWN_TOP_FIELDS = ("safety", "check", "check_timeout", "doc_only_paths")


def _has_unrecognized_changes(previous: dict, proposed: dict) -> bool:
    """True if `previous`/`proposed` differ anywhere this module doesn't already summarize by name
    (#109: a diff that mixes a known field with a brand-new one, e.g. a future `safety.<key>` or a
    new top-level key, used to disappear entirely behind the known-field summary — the human would
    approve a change they never saw). Checked by stripping every known key from both payloads
    first, then comparing what's left over field-by-field, rather than comparing full dicts, since
    a proposal only ever *replaces* config wholesale — a value merely reordered or defaulted
    differently in a key we don't track would otherwise register as "unrecognized" every time."""
    prev_safety = {k: v for k, v in ((previous or {}).get("safety") or {}).items()
                   if k not in _KNOWN_SAFETY_FIELDS}
    next_safety = {k: v for k, v in ((proposed or {}).get("safety") or {}).items()
                   if k not in _KNOWN_SAFETY_FIELDS}
    if prev_safety != next_safety:
        return True
    prev_top = {k: v for k, v in (previous or {}).items() if k not in _KNOWN_TOP_FIELDS}
    next_top = {k: v for k, v in (proposed or {}).items() if k not in _KNOWN_TOP_FIELDS}
    return prev_top != next_top


def _safety_baseline_changes(previous: dict, proposed: dict) -> list[str] | None:
    """Summarize gates.py guard_config's previous vs. proposed safety/merge-check config as a
    handful of human-readable fragments — never a bare count, since the human is approving a
    change, not being handed a raw diff to parse themselves.

    Returns `None` (not `[]`) when the two payloads are genuinely identical, so the caller can
    tell "no changes" apart from "changes we don't know how to summarize" (#85 round 2: the old
    code conflated the two and silently hid unrecognized changes behind "no changes detected").
    Callers must pair this with `_has_unrecognized_changes` (#109): a non-empty result here is the
    *known* diff only, and never a guarantee that it's the *whole* diff."""
    if previous == proposed:
        return None
    prev_safety = (previous or {}).get("safety") or {}
    next_safety = (proposed or {}).get("safety") or {}
    changes = []
    for field, label in (("protected", "protected"), ("remotes", "remotes"),
                         ("secret_allow", "secret_allow")):
        diff = _diff_named_list(prev_safety.get(field), next_safety.get(field))
        if diff:
            changes.append(f"{label}: {diff}")
    role_diff = _diff_roles(prev_safety.get("roles"), next_safety.get("roles"))
    if role_diff:
        changes.append("roles: " + "; ".join(role_diff))
    for field in ("check", "check_timeout"):
        before, after = (previous or {}).get(field), (proposed or {}).get(field)
        if before != after:
            changes.append(f"{field}: {json.dumps(before)} → {json.dumps(after)}")
    doc_only_diff = _diff_named_list((previous or {}).get("doc_only_paths"), (proposed or {}).get("doc_only_paths"))
    if doc_only_diff:
        changes.append(f"doc_only_paths: {doc_only_diff}")
    return changes


def render_card(question: dict) -> str:
    """Markup tags (`[b]`, `[dim]`, ...) are ours; every field that came from a question or a task
    result is escaped, since it can contain a literal `[...]` (a spec ref, a path, agent prose)."""
    q = question
    title = f"[b]#{q['id']}[/b] " + ("[bold red]⚠ SAFETY APPROVAL[/bold red]" if is_safety(q) else q["kind"].upper())
    if pin_rank(q) is not None:
        # design/system.md "Pinning & priority order": color never carries meaning alone
        # (REQ-TUI-011), so the red left bar (QuestionCard's `pinned-card` CSS class) always comes
        # with this word too.
        title += " [red]· Pinned[/red]"
    lines = [title]
    lines.append(escape(q["question"]))
    skip_context = False
    if is_safety(q):
        approval = q.get("approval") or {}
        if approval.get("paths"):
            lines.append("[dim]protected:[/dim] " + escape(", ".join(approval["paths"])))
        if "previous" in approval:
            # The safety/merge-check baseline approval (gates.py guard_config) — its `context` is
            # a raw "Approved:\n<json>\nProposed:\n<json>" dump that renders "Approved:\nnull" when
            # there's no previous baseline (#85); replace it with real copy instead of showing it.
            skip_context = True
            previous, proposed = approval.get("previous"), approval.get("proposed") or {}
            if previous is None:
                lines.append("[dim]baseline:[/dim] first approval — nothing to compare against yet")
                proposed_safety = proposed.get("safety") or {}
                for field, label in (("protected", "protected"), ("remotes", "remotes"),
                                     ("secret_allow", "secret_allow")):
                    items = proposed_safety.get(field) or []
                    if items:
                        lines.append(f"[dim]{label}:[/dim] " + escape(", ".join(sorted(items))))
                roles = proposed_safety.get("roles") or {}
                if roles:
                    lines.append("[dim]roles:[/dim] " + escape(json.dumps(roles, sort_keys=True)))
                check = proposed.get("check")
                if check:
                    lines.append("[dim]check:[/dim] " + escape(str(check)))
            else:
                changes = _safety_baseline_changes(previous, proposed)
                if changes is None:
                    lines.append("[dim]baseline:[/dim] re-approval requested (no changes detected)")
                else:
                    if changes:
                        lines.append("[dim]changed:[/dim] " + escape("; ".join(changes)))
                    # #109: a known-field diff is never a guarantee it's the *whole* diff — a
                    # proposal that mixes a recognized change (say, a new remote) with one this
                    # module doesn't know how to summarize (a brand-new safety.* key, or a new
                    # top-level key) used to show only the recognized half and hide the rest. If
                    # anything unrecognized changed too — or *only* unrecognized fields changed,
                    # the `changes == []` case the old code silently mislabeled "no changes" — show
                    # the raw Approved/Proposed context alongside whatever summary exists.
                    if not changes or _has_unrecognized_changes(previous, proposed):
                        lines.append("[dim]other changes:[/dim]")
                        lines.append(escape(q.get("context", "")))
    if q.get("context") and not skip_context:
        lines.append(escape(q["context"]))
    for i, opt in enumerate(q.get("options") or [], start=1):
        if i <= 9:
            lines.append(f"  [b]{i}[/b]. {escape(opt)}")
        else:
            lines.append(f"  • {escape(opt)}")
    return "\n".join(lines)


class QuestionCard(ListItem):
    """One Needs-you card. Safety-kind cards get a distinct border and can't be dismissed outright —
    dismissing one is a Reject (api.py forbids `dismiss_question` for approval-kind questions)."""

    DEFAULT_CSS = """
    QuestionCard { padding: 1; border: round $panel; }
    QuestionCard.safety-card { border: heavy $error; }
    QuestionCard.pinned-card { border-left: heavy $error; }
    QuestionCard:focus-within { border: round $accent; }
    """

    def __init__(self, question: dict) -> None:
        classes = "safety-card" if is_safety(question) else "question-card"
        if pin_rank(question) is not None:
            classes += " pinned-card"
        super().__init__(classes=classes)
        self.question = question

    @property
    def is_safety(self) -> bool:
        return is_safety(self.question)

    def compose(self) -> ComposeResult:
        yield Static(render_card(self.question))

    def refresh_content(self) -> None:
        self.query_one(Static).update(render_card(self.question))


class NeedsYouPane(Widget):
    """Open questions/ideas/safety cards.

    `a` arms answering **on the card focused at that moment** — the target id is captured then, not
    re-read at Enter, so a live removal elsewhere (another card answered, an event reshuffling the
    list) can never redirect the answer to whatever happens to be highlighted later (QA #67 bug: a
    human approving what they think is one safety card could otherwise approve a different one).
    `a` + a number + Enter picks that option; `a` + free text + Enter sends it verbatim — there is no
    instant-digit shortcut, so a reply that happens to start with a digit (or contain "d"/"a") is
    never partially interpreted as a keybinding (QA #67 bug: "3 but only after lunch" used to answer
    instantly on "3", then "after" re-armed answering and "fter lunch" fired as a second answer).
    `d` dismisses a regular card; on a safety card it asks "Reject #N? y/N" first, the same
    confirm-before-protected-action pattern as the kill switch (`s`, REQ-TUI-020)."""

    PANE_TITLE = "Needs you"  # #77: shown as this pane's 80x24 tab title (tui/app.py); was falling
    # back to the raw class name

    DEFAULT_CSS = """
    NeedsYouPane { height: 1fr; }
    NeedsYouPane > #ny-status { height: 1; color: $error; }
    """

    BINDINGS = [
        Binding("a", "start_answer", "Answer", show=True),
        Binding("d", "dismiss_focused", "Dismiss", show=True),
        Binding("y", "confirm_yes", "Confirm reject", show=False),
        Binding("n", "confirm_no", "Cancel", show=False),
        Binding("escape", "cancel_answer", "Cancel", show=False),
        Binding("r", "retry", "Retry", show=False),
    ]

    answering: reactive[bool] = reactive(False)

    def __init__(self, client: Any) -> None:
        super().__init__()
        self.client = client
        self.status = ""
        self._answering_id: int | None = None
        self._confirm_reject_id: int | None = None
        self._load_error_shown = False
        self._coalescer = ReloadCoalescer(self._attempt_load)
        self._retrier = AutoRetrier(self.load)

    def compose(self) -> ComposeResult:
        yield Static("Needs you", classes="pane-title")
        yield ListView(id="ny-cards")
        yield Input(placeholder="a number, or a reply, then Enter — Esc to cancel", id="ny-answer")
        yield Static("", id="ny-status")

    def on_mount(self) -> None:
        self.query_one("#ny-answer", Input).display = False

    def check_action(self, action: str, parameters: tuple) -> bool | None:
        """Keeps `y`/`n` inert (and free to bubble to some future global binding, e.g. copy) except
        while a safety-reject confirmation is actually pending; `a`/`d` inert with no focused card or
        mid-flow, so they can't stack on top of an answer or confirmation already in progress."""
        if action in ("confirm_yes", "confirm_no"):
            return self._confirm_reject_id is not None
        if action in ("start_answer", "dismiss_focused"):
            return not self.answering and self._confirm_reject_id is None and self._focused_card() is not None
        return True

    # ── loading + live updates ───────────────────────────────────────────
    async def load(self) -> None:
        # #108: coalesced (a burst of question.* events collapses to one in-flight + one trailing
        # reload) -- see panes/__init__.py's Pane.load(). #ny-status is its own small status line,
        # separate from #ny-cards, so a failed reload here already can't wipe the visible cards.
        await self._coalescer.trigger()

    async def _attempt_load(self) -> None:
        try:
            result = await self.client.call("questions", timeout=CALL_TIMEOUT, status="open", limit=1000)
        except Exception as e:
            reason = str(e) or type(e).__name__
            self._load_error_shown = True
            self._set_status(f"couldn't load: {reason} (r to retry)")
            self._retrier.schedule()
            return
        self._retrier.reset()
        if self._load_error_shown:
            # Only clear #ny-status if a load failure is what's showing there -- it's shared with
            # _call_safely's own action-failure messages, which a background resync must not stomp.
            self._load_error_shown = False
            self._set_status("")
        await self._set_cards(result.get("items", []))

    async def action_retry(self) -> None:
        await self.load()

    async def _set_cards(self, items: list[dict]) -> None:
        list_view = self.query_one("#ny-cards", ListView)
        focused_id = self._focused_question_id()
        await list_view.clear()
        for q in sort_cards(items):
            await list_view.append(QuestionCard(q))
        # ListView.index only auto-tracks children present at construction time, not appended ones,
        # so a freshly (re)populated list needs its highlight set explicitly.
        if not list_view.children:
            return
        if focused_id is not None:
            for i, item in enumerate(list_view.children):
                if isinstance(item, QuestionCard) and item.question["id"] == focused_id:
                    list_view.index = i
                    return
        list_view.index = 0

    def on_troupe_event(self, event: dict) -> None:
        name = event.get("event")
        if name not in ("question.new", "question.answered"):
            return
        q = (event.get("data") or {}).get("question")
        if q:
            self.run_worker(self._apply_update(q), exclusive=False)

    async def _apply_update(self, q: dict) -> None:
        list_view = self.query_one("#ny-cards", ListView)
        focused_id = self._focused_question_id()
        for item in list(list_view.children):
            if isinstance(item, QuestionCard) and item.question["id"] == q["id"]:
                if q["status"] != "open":
                    await item.remove()
                    if q["id"] == self._confirm_reject_id:
                        self._confirm_reject_id = None
                        self._set_status(f"#{q['id']} was answered elsewhere.")
                    # Removing an item can shift everyone below it up one slot; re-find whichever
                    # card was actually highlighted so the human's attention doesn't silently land
                    # on a different card (the visual half of the same bug as the answer-target fix).
                    self._restore_highlight(focused_id)
                else:
                    item.question = q
                    item.refresh_content()
                return
        if q.get("status") == "open":
            was_empty = not list_view.children
            # A newly-arrived card (e.g. a live safety approval) must land in its sorted position,
            # not just at the end — re-deriving the order from the current children plus the new
            # card keeps a live insert consistent with _set_cards's own initial ordering, without
            # assuming anything about event arrival order.
            existing = [item.question for item in list_view.children if isinstance(item, QuestionCard)]
            ordered_ids = [item["id"] for item in sort_cards(existing + [q])]
            await list_view.insert(ordered_ids.index(q["id"]), [QuestionCard(q)])
            if was_empty:
                list_view.index = 0
            else:
                # Inserting above the focused card (a pinned arrival jumping the queue) shifts its
                # position — unlike the pre-#109 append-only path, where the end never moved
                # anything. Re-anchor by id, the same fix the removal path already applies.
                self._restore_highlight(focused_id)

    def _restore_highlight(self, focused_id: int | None) -> None:
        if focused_id is None:
            return
        list_view = self.query_one("#ny-cards", ListView)
        for i, item in enumerate(list_view.children):
            if isinstance(item, QuestionCard) and item.question["id"] == focused_id:
                list_view.index = i
                return

    # ── focus helpers ────────────────────────────────────────────────────
    def _focused_card(self) -> QuestionCard | None:
        item = self.query_one("#ny-cards", ListView).highlighted_child
        return item if isinstance(item, QuestionCard) else None

    def _focused_question_id(self) -> int | None:
        card = self._focused_card()
        return card.question["id"] if card else None

    def _find_card_by_id(self, question_id: int) -> QuestionCard | None:
        for item in self.query_one("#ny-cards", ListView).children:
            if isinstance(item, QuestionCard) and item.question["id"] == question_id:
                return item
        return None

    # ── answer flow ──────────────────────────────────────────────────────
    def action_start_answer(self) -> None:
        card = self._focused_card()
        if self.answering or self._confirm_reject_id is not None or card is None:
            return
        self.answering = True
        self._answering_id = card.question["id"]
        answer_input = self.query_one("#ny-answer", Input)
        answer_input.value = ""
        answer_input.display = True
        answer_input.focus()

    def action_cancel_answer(self) -> None:
        if self.answering:
            self.answering = False
            self._answering_id = None
            answer_input = self.query_one("#ny-answer", Input)
            answer_input.display = False
            self.query_one("#ny-cards", ListView).focus()
        elif self._confirm_reject_id is not None:
            self._confirm_reject_id = None
            self._set_status("")

    async def action_dismiss_focused(self) -> None:
        card = self._focused_card()
        if card is None:
            return
        if card.is_safety:
            self._confirm_reject_id = card.question["id"]
            self._set_status(f"Reject #{card.question['id']}? y/N")
        else:
            await self._call_safely("dismiss_question", id=card.question["id"])

    async def action_confirm_yes(self) -> None:
        question_id = self._confirm_reject_id
        if question_id is None:
            return
        self._confirm_reject_id = None
        card = self._find_card_by_id(question_id)
        if card is None:
            self._set_status(f"#{question_id} was answered elsewhere.")
            return
        await self._submit(card, "Reject")

    def action_confirm_no(self) -> None:
        self._confirm_reject_id = None
        self._set_status("")

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id != "ny-answer":
            return
        question_id = self._answering_id
        text = event.value.strip()
        self.action_cancel_answer()
        if question_id is None or not text:
            return
        card = self._find_card_by_id(question_id)
        if card is None:
            self._set_status(f"#{question_id} was answered elsewhere.")
            return
        options = card.question.get("options") or []
        if text.isdigit() and 1 <= int(text) <= min(9, len(options)):
            text = options[int(text) - 1]
        await self._submit(card, text)

    async def _submit(self, card: QuestionCard, text: str) -> None:
        if card.is_safety:
            decision, note = split_decision(text)
            if decision not in ("approve", "reject"):
                self._set_status(f"Safety cards need Approve or Reject, not {text!r}.")
                return
            await self._call_safely("answer_question", id=card.question["id"], decision=decision, text=note)
        else:
            await self._call_safely("answer_question", id=card.question["id"], text=text)

    async def _call_safely(self, method: str, **params: Any) -> None:
        try:
            await self.client.call(method, timeout=CALL_TIMEOUT, **params)
            self._set_status("")
        except Exception as exc:  # the API, or the socket, can fail for any reason — never crash the pane
            code = getattr(exc, "code", None)
            if code == "unavailable":
                self._set_status(f"Not available yet: {exc}")
            else:
                self._set_status(f"{method} failed: {exc}")

    def _set_status(self, text: str) -> None:
        self.status = text
        self.query_one("#ny-status", Static).update(text)
