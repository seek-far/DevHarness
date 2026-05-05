"""Exclude fixture source trees from pytest collection.

Each fixture under tests/llm_fixtures/<name>/source/ is its own self-contained
project that the bug-fix agent runs against. Those test files (test_calc.py,
test_validator.py, …) have colliding basenames and are not part of this
project's own test suite. The driver in run_fixtures.py invokes pytest for
each fixture inside its working copy, where there is no collision.
"""

collect_ignore_glob = ["*/source/*", "*/source/**/*"]
