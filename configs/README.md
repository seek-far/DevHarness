# Agent Configurations

Each `*.json` file is a list of agent specs. The evaluation runner reads one of these and instantiates each spec as a separate agent for the sweep.

```bash
# Run baseline (no enhancements) on all fixtures:
python -m evaluation.cli run --config configs/baseline.json

# Compare baseline vs an enhancement (when one exists):
python -m evaluation.cli run --config configs/memory_vs_baseline.json
python -m evaluation.cli report <run_id>
```

## Spec shape

```json
{
  "name": "human-readable label, used in run dirs and reports",
  "agent": "agent kind known to the runner factory (e.g. 'langgraph')",
  "kwargs": { "...": "..." },
  "notes": "free-text"
}
```

## Adding a new config

1. Pick a name that's unique within the file.
2. Copy `baseline.json` and add or modify entries.
3. If introducing a new `agent` kind (e.g. wrapping a third-party agent), extend `evaluation/runner.py:make_agent`.

## Existing configs

| File | Purpose |
|---|---|
| `baseline.json` | LangGraphAgent with no enhancements — the reference point |
