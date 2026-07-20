"""
Phase templates for the staged (multi-phase) mini-swe-agent workflows.

The `workflow_mode` integer on MiniSweAgent selects the workflow:

  0 = mini default          — a single ReAct loop (mini's own swebench.yaml).
  1 = two-phase waterfall    — Investigate → Solve, defined here.
  2 = mode 0 + stage report  — see STAGE_REPORT_SUFFIX + mini_stage_model.py.
  3 = mode 1 + back-edge      — Investigate ⇄ Solve: Solve may request another
                                Investigate round (bounded); richer handoff +
                                structured re-investigation request (V3 templates).
  4 = mode 0 + background     — single ReAct loop with per-instance background
                                knowledge appended after the problem statement.
  (future modes increment; keep old numbers stable so eval comparisons hold.)

Design (see docs/swebench.md → "Staged workflows"): the two phases share ONE
docker container, so the WORLD state (source edits, the repro script the
Investigate phase writes) carries forward — only the CONVERSATION is dropped
between phases. The handoff is a small STRUCTURED artifact the Investigate phase
`submit`s via the normal sentinel; the Solve phase starts with a fresh message
list seeded by it, and can always re-read the actual files (ground truth is
never the summary). Pure waterfall for now: no back-edge from Solve to
Investigate (a v2 concern).

These templates are v1 and deliberately tunable — they are the main quality
lever of the staged workflow.
"""

from __future__ import annotations

# Per-phase step budgets. Investigation should not need the full 250-step budget
# a full fix loop gets; keeping it tight is part of the point (short context).
INVESTIGATE_STEP_LIMIT = 60
SOLVE_STEP_LIMIT = 250


# ── Mode 2: stage-annotated single loop (for measuring back-edges) ────────────
# Mode 2 IS mode 0 (mini's single ReAct loop) with one addition: the LLM reports
# which of the 5 recommended workflow stages it is in each turn. That turns the
# trajectory into an exact record of stage transitions — a later stage → an
# earlier one is a self-directed "back-edge" — instead of inferring control flow
# from bash-command heuristics.
WORKFLOW_STAGES = {
    1: "Analyze the codebase by finding and reading relevant files",
    2: "Create a script to reproduce the issue",
    3: "Edit the source code to resolve the issue",
    4: "Verify your fix works by running your script again",
    5: "Test edge cases to ensure your fix is robust",
}

STAGE_REPORT_SUFFIX = """

## Stage reporting (required for this run)

The `bash` tool has an extra REQUIRED argument `stage` — an integer 1-5 naming
the workflow stage the command belongs to:

1. Analyze the codebase by finding and reading relevant files
2. Create a script to reproduce the issue
3. Edit the source code to resolve the issue
4. Verify your fix works by running your script again
5. Test edge cases to ensure your fix is robust

Set `stage` on EVERY bash call to the stage you are ACTUALLY in. You may be in
any stage at any point, including going back to an earlier one — report it
honestly. This does not change how you work; only add the `stage` argument.
"""

# ── Phase 1: Investigate (analyze + reproduce; produce a handoff, NOT a fix) ──

INVESTIGATE_SYSTEM_TEMPLATE = (
    "You are a software engineer investigating a bug. You interact with a "
    "computer shell by issuing commands and reading their output."
)

INVESTIGATE_INSTANCE_TEMPLATE = """\
<pr_description>
Consider the following PR description:
{{task}}
</pr_description>

<instructions>
# Phase 1 of 2 — INVESTIGATE (do NOT fix anything yet)

Your job in THIS phase is to (a) locate the root cause and (b) create a script
that reproduces the issue. You are working in /testbed. You will hand off to a
second phase that writes the actual fix — so DO NOT edit source files here.

For each response: include a short THOUGHT, then issue at least one bash tool
call. Directory/env changes do not persist between commands; prefix with
`cd /testbed && ...` as needed.

## What to do
1. Explore the codebase (find/grep/read) to locate the file(s) and function(s)
   responsible. Read enough to form a concrete root-cause hypothesis.
2. Write a reproduction script to `/testbed/repro.py` (or a shell script) that
   demonstrates the bug, and run it to confirm it fails as described. Keep it
   minimal. This file WILL persist into the next phase.
3. Do NOT modify non-test source files in this phase.

## Submission (hand off to Phase 2)
When you have a root cause and a confirmed reproduction, write your handoff to
`/testbed/handoff.md` with EXACTLY these sections, then submit it:

```
ROOT_CAUSE: <one or two sentences: what's wrong and why>
SUSPECT_FILES: <comma-separated repo-relative paths most likely needing edits>
KEY_CODE: <function/class names and line hints the fixer should look at>
REPRO: <the exact command that reproduces the failure, e.g. `python repro.py`>
REPRO_OBSERVED: <the failing behavior you saw when you ran REPRO>
NOTES: <anything else the fixer needs; keep it short>
```

Submit with these SEPARATE commands (not combined with &&):
Step 1: `cat /testbed/handoff.md`   (verify it looks right)
Step 2 (EXACT): `echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT && cat /testbed/handoff.md`

You CANNOT continue after submitting. Submit only once the reproduction confirms
the bug.
</instructions>
"""

# ── Phase 2: Solve (fix + verify + harden; produce the patch) ─────────────────

SOLVE_SYSTEM_TEMPLATE = (
    "You are a software engineer fixing a bug. You interact with a computer "
    "shell by issuing commands and reading their output."
)

SOLVE_INSTANCE_TEMPLATE = """\
<pr_description>
Consider the following PR description:
{{task}}
</pr_description>

<handoff_from_investigation>
A previous investigation phase (same working tree in /testbed) produced this
handoff. Its reproduction script and notes files still exist on disk. Treat the
handoff as a strong lead, NOT as ground truth — re-read the actual files to
confirm before and after editing.

{{handoff}}
</handoff_from_investigation>

<instructions>
# Phase 2 of 2 — SOLVE (edit, verify, harden)

Working directory is /testbed. For each response: a short THOUGHT, then at least
one bash tool call. Directory/env changes do not persist between commands.

## What to do
1. Re-read the suspect file(s) named in the handoff to confirm the root cause.
2. Edit ONLY non-test source files to fix the issue in a general, codebase-
   consistent way. DO NOT modify tests or config files (pyproject.toml, etc.).
3. Verify by re-running the reproduction command from the handoff; iterate until
   it passes.
4. Consider edge cases and make the fix robust.

## Submission
When the fix is complete and verified, submit it as a git patch of ONLY the
source files you changed. Use SEPARATE commands (not combined with &&):

Step 1: `git -C /testbed diff -- <files you changed> > /testbed/patch.txt`
        (list only the source files you modified; do NOT commit; exclude the
         repro/handoff files and any tests you created)
Step 2: `cat /testbed/patch.txt`   (verify it contains only intended changes)
Step 3 (EXACT): `echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT && cat /testbed/patch.txt`

If the submit command exits nonzero it will not submit. You CANNOT continue
after submitting.
</instructions>
"""


def phase_agent_config(base_agent: dict, *, system_template: str, instance_template: str,
                       step_limit: int, output_path=None) -> dict:
    """Derive a per-phase mini AgentConfig from the base agent config, overriding
    the templates and step budget (and per-phase trajectory output_path) while
    preserving everything else (cost_limit, mode, …). Never mutates `base_agent`."""
    cfg = dict(base_agent or {})
    cfg["system_template"] = system_template
    cfg["instance_template"] = instance_template
    cfg["step_limit"] = step_limit
    if output_path is not None:
        cfg["output_path"] = output_path
    return cfg


# ── Mode 3: two-phase with bounded back-edge (Investigate ⇄ Solve) ─────────────
# mode 3 = mode 1 + (a) Solve may request ANOTHER Investigate round instead of
# submitting a patch, bounded by MAX_BACK_EDGES; (b) richer handoff (adds
# DETAILED_SUMMARY) + a structured re-investigation request. The back-edge is
# detected by a marker line at the start of Solve's submission (the model writes
# it to a file via bash — a reliable action, unlike a free-text `content` line).
MAX_BACK_EDGES = 2
REINVEST_MARKER = "REQUEST_REINVESTIGATION"

# Investigate (V3): same job as mode 1, but the handoff gains a DETAILED_SUMMARY
# section, and on a re-investigation round it is seeded with the previous handoff
# + the Solve phase's structured request.
INVESTIGATE_INSTANCE_TEMPLATE_V3 = """\
<pr_description>
Consider the following PR description:
{{task}}
</pr_description>
{% if reinvest_request %}
<reinvestigation_request>
A previous fix attempt (same /testbed working tree) got STUCK and asked for
another investigation round. Your earlier handoff was:

{{prior_handoff}}

The fix attempt reported:

{{reinvest_request}}

Focus this round on resolving what it got stuck on (re-localize, dig deeper),
then produce an UPDATED, MORE DETAILED handoff.
</reinvestigation_request>
{% endif %}
<instructions>
# Phase: INVESTIGATE (do NOT fix anything yet)

Locate the root cause and create a script that reproduces the issue. Work in
/testbed. DO NOT edit non-test source files in this phase. For each response:
a short THOUGHT, then at least one bash tool call.

## What to do
1. Explore (find/grep/read) to locate the responsible file(s)/function(s) and
   form a concrete root-cause hypothesis.
2. Write a reproduction script to `/testbed/repro.py` and run it to confirm the
   failure. This file persists into the next phase.

## Submission (hand off)
Write your handoff to `/testbed/handoff.md` with EXACTLY these sections, then
submit it:

```
ROOT_CAUSE: <one or two sentences>
SUSPECT_FILES: <comma-separated repo-relative paths most likely needing edits>
KEY_CODE: <function/class names + line hints the fixer should look at>
REPRO: <exact command that reproduces the failure>
REPRO_OBSERVED: <the failing behavior you saw>
DETAILED_SUMMARY: <AS MUCH DETAIL AS POSSIBLE: what you explored, the exact
  relevant code, hypotheses considered and ruled out, why the root cause is what
  you say, edge cases to watch, and anything the fixer needs so it does NOT have
  to re-discover it. Be thorough — this is the fixer's primary context.>
NOTES: <anything else>
```

Submit with SEPARATE commands (not combined with &&):
Step 1: `cat /testbed/handoff.md`
Step 2 (EXACT): `echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT && cat /testbed/handoff.md`
</instructions>
"""

# Solve (V3): same as mode 1, but may request re-investigation instead of a patch
# (when allowed), via a structured request file starting with REINVEST_MARKER.
SOLVE_INSTANCE_TEMPLATE_V3 = """\
<pr_description>
Consider the following PR description:
{{task}}
</pr_description>

<handoff_from_investigation>
The investigation phase (same /testbed working tree; its repro script + notes
still exist on disk) produced this handoff. Treat it as a strong lead, NOT as
ground truth — re-read the actual files to confirm.

{{handoff}}
</handoff_from_investigation>

<instructions>
# Phase: SOLVE (edit, verify, harden)

Work in /testbed. For each response: a short THOUGHT, then at least one bash
tool call.

## What to do
1. Re-read the suspect file(s) to confirm the root cause.
2. Edit ONLY non-test source files to fix the issue generally. Do NOT modify
   tests or config files.
3. Verify by re-running the reproduction command from the handoff; iterate.
4. Consider edge cases and make the fix robust.

## Submission — choose ONE
### (a) Submit your fix (preferred)
Step 1: `git -C /testbed diff -- <changed source files> > /testbed/patch.txt`
Step 2: `cat /testbed/patch.txt`
Step 3 (EXACT): `echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT && cat /testbed/patch.txt`
{% if can_reinvestigate %}
### (b) Request another investigation round (you have {{back_edges_left}} left)
Use this ONLY if you are genuinely stuck — e.g. the real root cause is elsewhere,
the handoff's localization is wrong, or you need information the handoff lacks.
Write `/testbed/reinvest.md` whose FIRST line is EXACTLY `REQUEST_REINVESTIGATION`,
followed by these sections:

```
REQUEST_REINVESTIGATION
WHAT_I_TRIED: <the edits/approaches you attempted>
WHY_STUCK: <precisely why they did not work / what the handoff got wrong>
WHAT_TO_INVESTIGATE: <the specific question the next investigation must answer>
DETAILED_SUMMARY: <AS MUCH DETAIL AS POSSIBLE: everything you learned this round,
  files read, exact code, what you changed and its observed effect, dead ends —
  so the next investigation does NOT repeat your work.>
```

Then submit it (EXACT): `echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT && cat /testbed/reinvest.md`
{% else %}
(No investigation rounds remain — you MUST submit a patch this round.)
{% endif %}
You CANNOT continue after submitting.
</instructions>
"""
