"""TUI slice B (#67): Needs-you pane — open questions, ideas, and safety approval cards.

Talks to the engine only through the API client (REQ-TUI-002): `client.call(method, timeout=5.0,
**params) -> dict`. Built against that duck-typed shape rather than importing `tui.client` (#66,
built in parallel) — swap in the real `TuiClient` once it lands, no changes needed here.
"""
from __future__ import annotations

from typing import Any

from rich.markup import escape
from textual import events
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


class AnswerInput(Input):
    """The reply box for `a`-armed answering. A leading digit 1-9 (nothing typed yet) is an instant
    option pick, matching REQ-TUI-020's "1-9 or free text"; anything else is normal text entry. Once
    the human has started typing, digits are just characters — REQ-COM-025's "never fire while an
    input has focus" is otherwise automatic: Input's own key handling consumes and stops every
    printable key before it could reach the pane's digit-shortcut binding."""

    def __init__(self, pane: "NeedsYouPane", **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._pane = pane

    async def _on_key(self, event: events.Key) -> None:
        if not self.value and event.is_printable and event.character and event.character.isdigit() \
                and event.character != "0" and self._pane.digit_is_valid_option(event.character):
            event.stop()
            event.prevent_default()
            await self._pane.answer_by_digit(event.character)
            return
        await super()._on_key(event)


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
    """Open questions/ideas/safety cards. `a` then a number or free text + Enter answers the focused
    card (REQ-TUI-020); see AnswerInput for how a bare digit picks an option while typed text never
    does (REQ-COM-025). `d` dismisses — for a safety card that means Reject, since dismissing an
    approval outright is forbidden."""

    DEFAULT_CSS = """
    NeedsYouPane { height: 1fr; }
    NeedsYouPane > #ny-status { height: 1; color: $error; }
    """

    BINDINGS = [
        Binding("a", "start_answer", "Answer", show=True),
        Binding("d", "dismiss_focused", "Dismiss", show=True),
        Binding("escape", "cancel_answer", "Cancel", show=False),
    ]

    answering: reactive[bool] = reactive(False)

    def __init__(self, client: Any) -> None:
        super().__init__()
        self.client = client
        self.status = ""

    def compose(self) -> ComposeResult:
        yield Static("Needs you", classes="pane-title")
        yield ListView(id="ny-cards")
        yield AnswerInput(self, placeholder="a number, or a reply + Enter, Esc to cancel", id="ny-answer")
        yield Static("", id="ny-status")

    def on_mount(self) -> None:
        self.query_one("#ny-answer", AnswerInput).display = False

    # ── loading + live updates ───────────────────────────────────────────
    async def load(self) -> None:
        result = await self.client.call("questions", timeout=CALL_TIMEOUT, status="open", limit=1000)
        await self._set_cards(result.get("items", []))

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
            self._apply_update(q)

    def _apply_update(self, q: dict) -> None:
        list_view = self.query_one("#ny-cards", ListView)
        for item in list(list_view.children):
            if isinstance(item, QuestionCard) and item.question["id"] == q["id"]:
                if q["status"] != "open":
                    item.remove()
                else:
                    item.question = q
                    item.refresh_content()
                return
        if q.get("status") == "open":
            was_empty = not list_view.children
            list_view.append(QuestionCard(q))
            if was_empty:
                list_view.index = 0

    # ── focus helpers ────────────────────────────────────────────────────
    def _focused_card(self) -> QuestionCard | None:
        item = self.query_one("#ny-cards", ListView).highlighted_child
        return item if isinstance(item, QuestionCard) else None

    def _focused_question_id(self) -> int | None:
        card = self._focused_card()
        return card.question["id"] if card else None

    # ── answer flow ──────────────────────────────────────────────────────
    def action_start_answer(self) -> None:
        if self.answering or self._focused_card() is None:
            return
        self.answering = True
        answer_input = self.query_one("#ny-answer", AnswerInput)
        answer_input.value = ""
        answer_input.display = True
        answer_input.focus()

    def action_cancel_answer(self) -> None:
        if not self.answering:
            return
        self.answering = False
        answer_input = self.query_one("#ny-answer", AnswerInput)
        answer_input.display = False
        self.query_one("#ny-cards", ListView).focus()

    async def action_dismiss_focused(self) -> None:
        card = self._focused_card()
        if card is None:
            return
        if card.is_safety:
            await self._submit(card, "Reject")
        else:
            await self._call_safely("dismiss_question", id=card.question["id"])

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id != "ny-answer":
            return
        card = self._focused_card()
        text = event.value.strip()
        self.action_cancel_answer()
        if card is None or not text:
            return
        options = card.question.get("options") or []
        if text.isdigit() and 1 <= int(text) <= min(9, len(options)):
            text = options[int(text) - 1]
        await self._submit(card, text)

    def digit_is_valid_option(self, digit: str) -> bool:
        """Whether `digit` picks a real option on the focused card — else AnswerInput types it literally."""
        card = self._focused_card()
        if card is None:
            return False
        options = card.question.get("options") or []
        return 0 <= int(digit) - 1 < min(9, len(options))

    async def answer_by_digit(self, digit: str) -> None:
        """Called by AnswerInput when a bare leading digit picks a valid option in answering mode."""
        card = self._focused_card()
        if card is None:
            return
        options = card.question.get("options") or []
        text = options[int(digit) - 1]
        self.action_cancel_answer()
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
