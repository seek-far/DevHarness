"""Vendored subset of mini-swe-agent (MIT). See ``UPSTREAM.md``.

Only two classes are vendored — the ReAct loop and the docker environment —
because those are the two places SDLCMA needs to modify in order to make a
run resumable (see the W2 work item: per-step checkpointing + container
re-attach). Everything else (models, prompt templates, config loading) is
still consumed from the installed upstream package.

Import paths inside the vendored files are unchanged: they still import
``minisweagent.*`` for the protocols, exceptions and helpers. That is
deliberate — vendoring the two classes must not turn into forking the
library.
"""
