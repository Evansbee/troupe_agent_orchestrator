# troupe GUI — design system

Source of truth for the visual language of `src/troupe/gui/`. This documents what the code in
`theme.py` / `core.py` / `views.py` / `app.py` actually does today — treat divergence between this
doc and the code as a bug in one of the two. See specs/40-gui.md for behavioral requirements.

## Voice

Midnight command center. Dark, dense, a little bit alive — glows and particles imply the team is
working even when you're not looking directly at it. Never flashy for its own sake: motion always
carries a signal (running, new, selected, stale).

## Color

### Base surfaces (`theme.py`)
| Token | RGB | Use |
|---|---|---|
| `BG` | 9,11,16 | window clear color |
| `BG2` | 12,15,21 | board column background, top-bar gradient end |
| `PANEL` | 16,19,27 | default panel fill (sidebar, inbox, center, modals) |
| `PANEL2` | 22,26,36 | card fill (resting), chat bubble (agent), unselected chip |
| `PANEL3` | 30,35,48 | card/chip fill (hover), code fences |
| `HOVER` | white @ 5% | generic hover wash over any surface |
| `INPUT` / `INPUT_FOCUS` | 13,16,23 / 15,18,27 | text input fill, resting/focused |
| `TOOLTIP` | 28,32,44 @ 98% | tooltip + toast fill |
| `BORDER` / `BORDER_HI` | 34,39,54 / 52,59,80 | hairlines; `_HI` on hover/focus/selected |

### Text
| Token | Use |
|---|---|
| `TEXT` | primary text, active tab, card titles |
| `TEXT_DIM` | secondary text, unselected tab, body copy in bubbles |
| `TEXT_FAINT` | metadata — timestamps, counts, placeholders, section labels |
| `ON_ACCENT` | text/icons drawn on a filled accent surface (always white) |

### Accent & semantic
| Token | RGB | Use |
|---|---|---|
| `ACCENT` | 124,140,255 (indigo) | primary actions, human chat bubbles, focus rings, active tab underline |
| `ACCENT2` | 167,139,250 (violet) | reserved — currently unused outside role palette overlap with designer role |
| `GREEN` | 52,211,153 | success / done / "Live" / working badges |
| `YELLOW` | 250,204,21 | paused / warning |
| `ORANGE` | 251,146,60 | throttled / review-needed / "parked, owes work" |
| `RED` | 248,113,113 | blocked / error / danger actions |
| `CYAN` | 56,189,248 | ready status |
| `PINK` | 244,114,182 | "needs you" badge, question cards |
| `CODE_BG` / `CODE_TEXT` | 24,28,40 / 196,205,255 | markdown code fences and inline code |
| `HUMAN` | 236,239,247 | the human's node/avatar (near-white, deliberately outside the role palette) |

Semantic colors are reused directly as status colors (`STATUS_COLORS`, `PRIORITY_COLORS` in
`theme.py`) — never invent a new hue for a new status; pick the closest existing meaning.

### Role colors (`roles.py`, not `theme.py`)
Each role owns one identity color, used everywhere that role appears (avatars, chat bubbles,
pills, graph nodes, sidebar). `data.color_of(agent_id)` resolves an agent to its role color;
`human` and `system` are special-cased to `T.HUMAN` / `T.TEXT_FAINT`.

| Role | Color |
|---|---|
| Team Lead | `(245,184,75)` amber |
| Product Manager | `(244,114,182)` pink |
| Spec Writer | `(56,189,248)` cyan |
| Designer | `(167,139,250)` violet |
| Builder | `(52,211,153)` green |
| QA / Tester | `(251,146,60)` orange |
| Gadfly | `(248,113,113)` red |

Role colors and semantic colors share hues (green = builder *and* success, orange = QA *and*
throttled, red = gadfly *and* error). This is intentional economy, not collision — the two systems
never appear ambiguously in the same glyph (a role color always sits on an avatar/name; a semantic
color always sits on a status pill/dot).

## Type

Faces (`core.py FACES`, loaded via `ui.font(face, size)`):
- `ui` — Inter Regular. Default body text.
- `med` — Inter Medium. Labels, buttons, names, anything that needs to stand slightly forward.
- `bold` — Inter SemiBold. Titles, headings, emphasis (`**bold**` in markdown).
- `mono` / `monob` — JetBrains Mono. IDs (`#12`), tool-call lines, code, transcript metadata.

Sizes in use today are ad hoc (anywhere from 10 to 21px, in ~0.5px steps, chosen per call site).
There is no `theme.py` type-scale constant yet — see polish task below. Until that lands, match the
nearest existing usage rather than picking a new number:

| Role | ~Size | Face | Example |
|---|---|---|---|
| Section label (all-caps) | 10–11 | bold | "TEAM", "TASKS", "ROLE" |
| Metadata / timestamp | 11–12 | ui/mono | `ago()` strings, run ids |
| Body / label | 12.5–13.5 | ui/med | card titles, bubble text, buttons |
| Emphasized body | 14 | ui/med | chat composer, question text |
| Card/section title | 14.5–16 | bold | memory card title, tab labels |
| View heading | 17–19 | bold | chat empty-state headline, logo |
| Page heading | 21 | bold | agent-detail name |

Line height: body text wraps at `1.45×` size (`text_block` default); markdown paragraphs at `1.5×`;
headings tighter at `1.35×`.

## Spacing & radius

Layout metrics live in `theme.py`: `TOP_H=56`, `SIDEBAR_W=272`, `INBOX_W=360`, `GAP=10` (the gutter
between shell regions). Card/panel internal padding is consistently `12–16px`; list-row gaps are
`6–10px`.

Corner radius has one named token, `RADIUS=12`, but it's used almost nowhere directly — every widget
picks its own literal instead:

| Radius | Where |
|---|---|
| 3 | inline code background, checkbox in markdown |
| 6 | tooltips |
| 7 | buttons |
| 9 | chips, task/memory cards, board columns, text inputs |
| 10 | panels (sidebar/inbox/center), mail rows |
| 12 | `T.RADIUS` — declared, effectively unused |
| 13 | pills computed as `h/2` (fully rounded), active chip |
| 14 | chat bubbles, modals |
| `h/2` | pills, badges — always fully rounded |

Treat this table as the de facto scale (buttons=7, list-item=9, container=10, bubble/modal=14) until
the polish task below turns it into real tokens.

## Motion

Everything animates through `ui.ease(key, target, speed)` — an exponential approach
(`lerp(v, target, dt·speed)`), never a fixed-duration tween. Higher `speed` = snappier.

| Speed | Use |
|---|---|
| 8 | scrollbar thumb fade |
| 14 | button hover fill |
| 16 | card hover lift, panel-scroll offset |
| 18 | tab underline slide, sidebar card hover |

Ambient motion (not input-driven) is layered separately:
- **Working ring**: a rotating arc + soft pulse (`sin(t·3.2)`) around any avatar mid-run (`ui.avatar(..., running=True)`).
- **Pulse graph particles**: messages fly as easing dots along a quadratic bezier between sender and
  recipient over 1.4s, leaving a heat-glow on the edge that fades over 10 minutes.
- **Live pill pulse**: the top-bar "Live" dot breathes (`sin(t·3)`) — the only chrome-level idle animation.
- **Toasts**: fade+slide in/out over the first/last ~0.2–0.5s of a 6s lifetime.

The app throttles to 20fps when idle (`ui.activity` > 4s and nothing running/animating) and 60fps
otherwise — animations must stay legible at both.

## Components

- **Panel** (`ui.panel`) — filled rounded rect + 1px border. The base container for every region.
- **Button** (`ui.button`) — kinds `primary` (filled accent), `default` (panel-tinted), `ghost`
  (transparent → hover wash), `danger` (red-tinted). Hover eases a fill/border blend; label always
  centered.
- **Pill** (`ui.pill`) — static, fully-rounded label chip. `filled=True` for a solid-color badge
  (priority on modals), `filled=False` (default) for a 16%-alpha tint (status, role tags). Not
  interactive.
- **Chip** (`ui.chip`) — interactive, fully-rounded toggle/filter button with an `active` state
  (colored tint + stroke) vs resting `PANEL2`/`PANEL3` on hover. Used for filters (Mail, Memory,
  Pulse feed), suggestions (chat empty state), and inline pickers (new-task role/priority).
- **Badge** (`ui.badge`) — small filled numeric circle for unread/pending counts, overlaid on an
  avatar or label corner.
- **Avatar** (`ui.avatar`) — ringed circle with role-colored initials; grows a pulsing glow + rotating
  arc while `running`, desaturates toward `BG` when `dim` (disabled).
- **Card** — no single `ui.card()`; task cards (`views._task_card`) and memory cards
  (`views.memory_view`) share the pattern: `PANEL2`→`PANEL3` on hover, `BORDER`→`BORDER_HI`, a 3px
  colored accent bar on the left edge when status/kind matters.
- **Chat bubble** (`views._bubble`) — right-aligned accent-tinted for the human, left-aligned
  `PANEL2` for agents; shrink-wraps to content width for short single-line messages instead of
  filling the max bubble width.
- **Tooltip** (`ui.tip` / `_draw_tooltip`) — dark rounded box following the cursor, shown on hover;
  used for keyboard shortcuts, truncated text, and status detail.
- **Toast** (`app.draw_toasts`) — bottom-right stack above the inbox column, colored dot + text,
  auto-dismiss after 6s.
- **Scrollbar** (`ui.scroll_end`) — thin inset thumb, only visible on hover/drag, everywhere content
  scrolls.
- **Text input** (`ui.text_input`) — multiline-capable, Enter submits / Shift+Enter newlines, focus
  ring eases border+fill toward `ACCENT`.
- **Markdown** (`ui.markdown` / `md_layout`) — headings, lists (bulleted/numbered/checkbox), block
  quotes, code fences, tables, inline `**bold**`/`` `code` ``/`[link](...)`/`_em_`. This is the
  rendering substrate for chat, mail, memory, docs, and task detail bodies — any copy in the app can
  assume markdown is safe to use.
- **Modal** (`views._task_modal`, `_new_task_modal`) — centered panel over a scrim, dismiss on
  click-outside or Esc.

## Known gaps (feeds the polish backlog below)

- No real type-scale or radius-scale constants — both are "whatever the nearest call site used."
- `STATUS_COLORS["backlog"]` and `STATUS_COLORS["cancelled"]` are both `TEXT_FAINT` — visually
  identical despite meaning opposite things.
- Empty states are inconsistent in polish: Chat and the human inbox get an illustrated
  glow+icon+copy treatment; Mail, Memory, Docs, and empty Board columns get a single gray line.
