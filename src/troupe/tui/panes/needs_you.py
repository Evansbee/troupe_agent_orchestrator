"""TUI slice B (#67): Needs-you pane — open questions, ideas, and safety approval cards.

Talks to the engine only through the API client (REQ-TUI-002): `client.call(method, timeout=5.0,
**params) -> dict`. Built against that duck-typed shape rather than importing `tui.client` (#66,
built in parallel) — swap in the real `TuiClient` once it lands, no changes needed here.
"""
from __future__ import annotations

from typing import Any

from rich.markup import escape
from textual.app import ComposeResult
from textual.binding import Binding
from textual.reactive import reactive
from textual.widget import Widget
from textual.widgets import Input, ListItem, ListView, Static

APPROVAL_KINDS = ("approval", "safety")
CALL_TIMEOUT = 5.0


def is_safety(question: dict) -> bool:
    return question.get("kind") in APPROVAL_KINDS


def split_decision(text: str) -> tuple[str, str]:
    """"Reject — too broad" -> ("reject", "too broad"); mirrors gates.py's own parsing."""
    head, _, note = text.partition(" — ")
    return head.strip().lower(), note.strip()


def render_card(question: dict) -> str:
    """Markup tags (`[b]`, `[dim]`, ...) are ours; every field that came from a question or a task
    result is escaped, since it can contain a literal `[...]` (a spec ref, a path, agent prose)."""
    q = question
    lines = [f"[b]#{q['id']}[/b] " + ("[bold red]⚠ SAFETY APPROVAL[/bold red]" if is_safety(q) else q["kind"].upper())]
    lines.append(escape(q["question"]))
    if is_safety(q):
        approval = q.get("approval") or {}
        if approval.get("paths"):
            lines.append("[dim]protected:[/dim] " + escape(", ".join(approval["paths"])))
    if q.get("context"):
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
    QuestionCard:focus-within { border: round $accent; }
    """

    def __init__(self, question: dict) -> None:
        super().__init__(classes="safety-card" if is_safety(question) else "question-card")
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
        # #108: a load failure here must not crash the app -- see chat.py's load() for why.
        try:
            result = await self.client.call("questions", timeout=CALL_TIMEOUT, status="open", limit=1000)
        except Exception as e:
            self.query_one("#ny-status", Static).update(f"couldn't load: {e or type(e).__name__} (r to retry)")
            return
        await self._set_cards(result.get("items", []))

    async def action_retry(self) -> None:
        await self.load()

    async def _set_cards(self, items: list[dict]) -> None:
        list_view = self.query_one("#ny-cards", ListView)
        focused_id = self._focused_question_id()
        await list_view.clear()
        for q in items:
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
            await list_view.append(QuestionCard(q))
            if was_empty:
                list_view.index = 0

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
