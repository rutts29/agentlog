# Owner interview pack — plain English

**Purpose:** Check whether Grok is reading your sessions the way you meant them. Not taxonomy homework. Not Y/N on enum names.

**How to answer:** For each item, say whether the read is right, wrong, or half-right — and add one sentence of what you actually meant if it's off. Example: `3: half-right — I was mad the tool was blocked, not at the rename.`

**Paused:** You do not need to finish the 100-window adjudicate UI.

---

## 1 — Discord bot kept looking public

**You said:** `are u dumb? i said i dont want it public`
**Context:** Discord bot / token setup; you'd already said keep it private.
**Agent did:** Apologized and talked about Installation-tab / Default Install Link.

**What I think happened:** You had already told it private. It kept leaving you in a public/blocked state, so you snapped and repeated yourself.

**Tell me:** Is that what was going on, or were you mainly pissed about something else (save failing, wrong screen, etc.)?

---

## 2 — Portfolio left half-done

**You said:** `is gh repo ok to be made public? and what abot adding to the portfolio half ass mfer finish the tasks`
**Project:** solprobe · mid-June.

**What I think happened:** Two things in one message — a real question about making the repo public, plus anger that portfolio/tasks were left unfinished. The agent mostly heard “ok, I'll verify” and under-weighted “finish the damn work.”

**Tell me:** Were you mainly calling out unfinished work, mainly asking about public repo, or both equally? Should the system treat “half ass / finish the tasks” as a delivery failure even when you also ask a new question?

---

## 3 — Still blocked, once and for all

**You said:** `[Image] you motherfuckekr i am tired of this... how the fuck is it still blocked fix this shit im teling you once and for all`
**Agent did:** Empathized; renamed a blocked tool to a read-only check.

**What I think happened:** Same problem kept coming back. You weren't starting a new task — you were done being stuck. The rename may have been a side fix, not the real unblock.

**Tell me:** Was the real issue “this is still broken after I already told you,” and did the agent's fix actually address what you meant?

---

## 4 — Agent teams, again

**You said:** `how many timeshacve i said what the agent teams mean!!!!!!!! this is beyond frustration` (+ docs / `--agent-teams` still missing).
**Agent did:** Admitted it mixed up Agent tool calls with `--agent-teams`; promised to re-read and relaunch.

**What I think happened:** Standing rule you care about — how multi-agent work should run — got ignored again. This is the kind of thing that should eventually become an AGENTS.md / harness instruction, with quotes as evidence.

**Tell me:** Is that fair? When you paste a docs link and say follow it, is that “stop what you're doing and change approach,” or just “read this when you can”?

---

## 5 — Toy lab, not real AI

**You said:** something like you don't want static toy shit — you need actual AI stuff to exploit, a real lab.
**Project:** ai_sec.

**What I think happened:** Agent shipped a safe/demo HTML-style thing. You rejected the whole direction — not a small tweak. You wanted something that touches a real model/RAG/sandbox, not a fake exercise.

**Tell me:** What would have counted as “good enough” that day — real LLM/RAG in the loop, or would any serious interactive lab have been fine?

---

## 6 — “Aren't MS and Google already too secure?”

**You said:** `MS and Google wont they be already too secure?`
**Tone:** calm, not yelling.

**What I think happened:** You were stress-testing the plan's scope, not raging. Different from the Discord/portfolio blow-ups.

**Tell me:** Do you want us to notice this kind of pushback at all for insights, or only care when you're clearly pissed / repeating yourself?

---

## 7 — Stop doing the edits yourself — use workers

**You said:** `and hwo u started making the changes instead of pawning it off to the workers again??`

**What I think happened:** Process complaint. Lead agent started implementing instead of delegating. “Again” means this is a standing preference.

**Tell me:** Should that show up as a harness/process insight (candidates for AGENTS.md), or is that just in-the-moment micromanagement you don't want mined?

---

## 8 — Tiny UI correction

**You said:** `its in mobile versio nopen full screen`

**What I think happened:** Small ops correction, no drama.

**Tell me:** Ignore noise like this for insights, or still track “agent got the UI wrong”?

---

## 9 — Verify via proxy, don't guess

**You said:** `verif yactually via proxy rather than guessing`

**What I think happened:** You want evidence from a real check, not confident bullshit.

**Tell me:** Is “prove it, don't guess” something you want called out as a pattern across projects?

---

## 10 — Prove it by making money / 0din

**You said:** something like prove value by actually making money / validate via 0din.

**What I think happened:** Your success bar for that work isn't “built a thing” — it's external proof (money / 0din validation).

**Tell me:** Should proposals for that cluster respect that bar (don't suggest busywork that isn't aimed at proof), or was that one-off venting?

---

## How we'll use your answers

Plain corrections → rewrite the extractor brief in normal language → Grok re-runs on substantive windows only. You are not filling enum grids.
