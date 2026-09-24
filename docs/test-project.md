# The first test project: proving troupe is "finished"

Owner: PM. Status: draft, waiting for the "Ready for a test project" milestone and the human's pick.

## Why
The vision says troupe is finished when the human can point it at an empty directory, describe a project,
and get working software while seeing what every agent is doing. troupe has only built itself so far.
The test project is the first real check of that claim.

## When
Start once the "Ready for a test project" milestone is merged: #1, #2, #3, #15, #20 (done); #24, #25,
#42, #43, #35 (in flight). The Swift GUI is not required.

## Candidate projects (the human picks one; PM recommends running A, then B)
| | Project | Why it's a good test |
|---|---|---|
| **A** | **A repeatable benchmark: "Links", a small self-hosted bookmark manager** (web UI + API + SQLite + tests + auth) | Small enough to finish in a day, and big enough to need every role: spec, design, 2–3 parallel builders, QA, the merge gate. We can rerun it after each troupe release and compare. |
| **B** | **Something the human actually wants.** Their pick, any size. | The real proof. Real taste, real ambiguity, and a real reason to care. |
| **C** | **A CLI tool** (e.g. a git-history "what changed this week" summarizer) | Lowest risk. Mostly exercises builders + QA, and less design/spec. |

## What we measure (so "finished" isn't a feeling)
- **Outcome:** does the software run and meet its own specs? QA's verification matrix + a human smoke test.
- **Human effort:** number of Needs-you cards, minutes of human chat, number of times the human had to intervene or unstick something.
  Lower is better, but a card that shaped the product is good, not bad.
- **Time:** empty dir → first runnable build → "done" (wall clock).
- **Cost:** runs, tokens and the provider-usage % consumed, split into coordination vs work (#46).
- **Visibility:** could the human always tell what was happening from Pulse / Work panel alone?
- **Safety:** zero secret exposure, zero out-of-scope actions, and every blocked action explained (Principle 0).

## Kickoff checklist
1. `mkdir <project> && cd <project> && troupe up` (after #24: runs as a service).
2. The PM interviews the human (vision in 10 minutes, not an hour), then spec + lead take over.
3. The dogfood troupe keeps running alongside. Two projects at once is itself part of the test (#25 handles; #29 later).
4. Afterwards: a retro written to `docs/retro-<project>.md`, and every friction point filed as a troupe task.
