"""Reviewed handler code, the only code a worker process will load.

A handler here is ordinary, code-reviewed Python in this repository. The
worker refuses to import anything outside this package, so a task, a
payload or a model can name which reviewed handler runs, never supply
the code (item 1.2, #48).
"""

from typing import Any


def register_all(supervisor: Any) -> None:
    """Register every reviewed handler on a supervisor started as a daemon.

    The proposal summarizer (``lab.loop``) is registered when a loopback
    model is configured through ``LAB_MODEL_URL``, ``LAB_MODEL_NAME`` and
    ``LAB_MODEL_REVISION``. The demo handlers exist for tests and are
    deliberately not reachable by tasks on a running lab. Add a
    ``register_reviewed`` call here in the same change that adds a handler.
    """
    from lab import loop

    model = loop.model_from_env()
    if model is not None:
        loop.register(supervisor, model)
