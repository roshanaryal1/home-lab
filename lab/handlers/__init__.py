"""Reviewed handler code, the only code a worker process will load.

A handler here is ordinary, code-reviewed Python in this repository. The
worker refuses to import anything outside this package, so a task, a
payload or a model can name which reviewed handler runs, never supply
the code (item 1.2, #48).

The first three local tools (#240), each with the fewest broker tools it
needs and a policy tier (the highest tier among those tools):

* ``workspace.files`` (``workspace.py``): read, list, write and search the
  task's own workspace. Notify.
* ``git.read`` (``git_read.py``): status, log and diff of a repository
  inside the workspace. Autonomous.
* ``web.summary`` (``web.py``): fetch one page through the egress gateway
  and have the bounded model summarize it. Notify.
"""

from __future__ import annotations

import os
from typing import Any

WEB_HOSTS_ENV = "LAB_WEB_FETCH_HOSTS"


def web_hosts_from_env() -> frozenset[str]:
    """The hosts ``web.summary`` may fetch from, set by the operator.

    A comma-separated list of DNS names (``*.example.org`` allowed) in
    ``LAB_WEB_FETCH_HOSTS``. Empty means the handler is not registered at
    all. An IP address or a malformed name raises, so a bad list stops the
    daemon at start rather than quietly widening or narrowing the network.
    """
    from lab.egress import parse_allowlist
    raw = os.environ.get(WEB_HOSTS_ENV, "")
    return parse_allowlist(h.strip() for h in raw.split(",") if h.strip())


def register_all(supervisor: Any) -> None:
    """Register every reviewed handler on a supervisor started as a daemon.

    The workspace and git handlers are always registered. The proposal
    summarizer (``lab.loop``) is registered when a loopback model is
    configured through ``LAB_MODEL_URL``, ``LAB_MODEL_NAME`` and
    ``LAB_MODEL_REVISION``; the web handler needs that model and a host
    list in ``LAB_WEB_FETCH_HOSTS`` as well. The demo handlers exist for
    tests and are deliberately not reachable by tasks on a running lab.
    Add a ``register_reviewed`` call here in the same change that adds a
    handler.
    """
    from lab import loop
    from lab.handlers import git_read, web, workspace

    supervisor.register_reviewed(workspace.KIND, workspace.REF, tools=workspace.TOOLS)
    supervisor.register_reviewed(git_read.KIND, git_read.REF, tools=git_read.TOOLS)

    model = loop.model_from_env()
    if model is not None:
        loop.register(supervisor, model)
        hosts = web_hosts_from_env()
        if hosts:
            supervisor.broker.set_summarizer(loop.evidence_summarizer(model))
            supervisor.register_reviewed(web.KIND, web.REF, tools=web.TOOLS,
                                         egress_hosts=hosts)
