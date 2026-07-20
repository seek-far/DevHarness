"""
StageReportingModel — the mode-2 model variant.

mode 2 measures self-directed back-edges by having the LLM report which of the 5
workflow stages each action belongs to. A free-text `STAGE: <n>` line does NOT
work with reasoning + tool-calling models (their visible `content` is empty; the
CoT is in `reasoning_content` and doesn't honour output-format instructions —
observed: deepseek-v4-pro reported 0/27 turns). So the stage rides in the
STRUCTURED function-call arguments instead: a required `stage` field on the bash
tool. The model cannot emit a valid tool call (i.e. cannot act) without it, so
compliance is schema-enforced, not prompt-dependent.

Scoped to mode 2 only (mini hardcodes the plain `BASH_TOOL` in `LitellmModel`;
mode 0/1 stay byte-identical). This subclass lives in OUR tree — mini's fork is
untouched. Selected via `get_model(config={..., "model_class":
"agents.mini_stage_model.StageReportingModel"})` in MiniSweAgent._build_model.
"""

from __future__ import annotations

import copy
import json

from minisweagent.models.litellm_model import LitellmModel
from minisweagent.models.utils.actions_toolcall import BASH_TOOL

# The plain bash tool + a required `stage` argument.
BASH_TOOL_WITH_STAGE = copy.deepcopy(BASH_TOOL)
BASH_TOOL_WITH_STAGE["function"]["parameters"]["properties"]["stage"] = {
    "type": "integer",
    "enum": [1, 2, 3, 4, 5],
    "description": (
        "The workflow stage this command belongs to: 1=analyze/locate relevant "
        "files, 2=reproduce the issue, 3=edit/fix the source, 4=verify the fix, "
        "5=test edge cases. Report the stage you are ACTUALLY in — you may return "
        "to an earlier stage."
    ),
}
BASH_TOOL_WITH_STAGE["function"]["parameters"]["required"] = ["command", "stage"]


class StageReportingModel(LitellmModel):
    def _query(self, messages, **kwargs):
        # Mirrors LitellmModel._query but sends the stage-augmented tool.
        import litellm

        try:
            return litellm.completion(
                model=self.config.model_name,
                messages=messages,
                tools=[BASH_TOOL_WITH_STAGE],
                **(self.config.model_kwargs | kwargs),
            )
        except litellm.exceptions.AuthenticationError as e:
            e.message += " You can permanently set your API key with `mini-extra config set KEY VALUE`."
            raise e

    def _parse_actions(self, response) -> list:
        # Reuse mini's parse (command + tool_call_id + FormatError handling), then
        # augment each action with its structured `stage` argument.
        actions = super()._parse_actions(response)
        tool_calls = response.choices[0].message.tool_calls or []
        for action, tc in zip(actions, tool_calls):
            try:
                action["stage"] = json.loads(tc.function.arguments).get("stage")
            except Exception:
                action["stage"] = None
        return actions
