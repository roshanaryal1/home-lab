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

Two more, granted by the owner on 2026-10-01 (#255, #256), each holding one
approve-tier tool, so every call waits for the operator's signature:

* ``skill.run`` (``skill_run.py``): run one script of an active skill in the
  container. Registered only when ``LAB_CONTAINER_IMAGE`` is set.
* ``mcp.call`` (``mcp_call.py``): call one tool of an operator-signed MCP
  server. Registered only when an MCP servers file is configured and every
  entry in it verifies.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

WEB_HOSTS_ENV = "LAB_WEB_FETCH_HOSTS"
CONTAINER_IMAGE_ENV = "LAB_CONTAINER_IMAGE"
MCP_SERVERS_ENV = "LAB_MCP_SERVERS"


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


def mcp_servers_file_from_env() -> Path | None:
    """The operator-signed MCP servers file the daemon reads, if any.

    ``LAB_MCP_SERVERS`` names a JSON list of signed entries (``lab.mcp``).
    Unset or empty means no MCP server is configured and ``mcp.call`` refuses
    every call. Unlike ``lab mcp``, the daemon has no default path: the
    operator turns MCP on by naming the file.
    """
    raw = os.environ.get(MCP_SERVERS_ENV, "").strip()
    return Path(raw) if raw else None


def signed_mcp_servers(supervisor: Any) -> frozenset[str]:
    """Every server in the configured MCP file, once each one verifies.

    Empty when no file is configured. An entry that is unsigned, badly signed
    or edited after signing, or any entry when there is no operator key,
    raises ``McpRefused``, so a file the operator has not signed stops the
    daemon at start rather than being granted to a handler.
    """
    from lab.mcp import McpRefused
    registry = supervisor.broker.mcp_registry
    if registry is None:
        return frozenset()
    states = {name: registry.state(name) for name in registry.names}
    unsigned = {name: state for name, state in states.items() if state != "signed"}
    if unsigned:
        raise McpRefused("the MCP servers file holds entries that do not verify: "
                         + ", ".join(f"{n} ({s})" for n, s in sorted(unsigned.items()))
                         + ". The operator must sign them, or remove them")
    return frozenset(states)


def configure_skill_runner(supervisor: Any) -> bool:
    """Turn on the broker's ``skill.run`` when an image is configured (#255).

    ``LAB_CONTAINER_IMAGE`` names the guest image, pinned by sha256 digest.
    Unset or empty means the tool stays off and refuses every call. An image
    that is not pinned raises, so a bad setting stops the daemon at start.
    No handler is granted the tool here: a handler that needs it says so
    in its own registration.
    """
    image = os.environ.get(CONTAINER_IMAGE_ENV, "").strip()
    if not image:
        return False
    from lab.container import (
        AppleContainerRuntime,
        ContainerConfig,
        ContainerExecutor,
        check_image,
    )
    from lab.skillstore import SkillStore

    broker = supervisor.broker
    executor = ContainerExecutor(AppleContainerRuntime(), broker.workspace_root,
                                 ContainerConfig(image=check_image(image)))
    broker.set_skill_runner(SkillStore(supervisor.queue._conn, supervisor.artifacts),
                            executor)
    return True


def register_all(supervisor: Any) -> None:
    """Register every reviewed handler on a supervisor started as a daemon.

    The workspace and git handlers are always registered. The proposal
    summarizer (``lab.loop``) and the chat answerer (``lab.chat``, no tools)
    are registered when a loopback model is configured through
    ``LAB_MODEL_URL``, ``LAB_MODEL_NAME`` and ``LAB_MODEL_REVISION``; the web
    handler needs that model and a host list in ``LAB_WEB_FETCH_HOSTS`` as
    well. They share one model and its heavy slot, a lock file beside the
    database that ``lab tick`` takes too. Without a model a chat task is
    cancelled with "no handler", and the chat says so. The demo handlers exist
    for tests and are deliberately not reachable by tasks on a running lab.
    With ``LAB_CONTAINER_IMAGE`` set, the broker's ``skill.run`` is turned
    on (``configure_skill_runner``) and the ``skill.run`` handler is granted
    it. With an MCP servers file configured (``LAB_MCP_SERVERS`` for the
    daemon) and every entry signed, the ``mcp.call`` handler is granted
    ``mcp.call`` and those servers, with no network. Both tools are approve
    tier. Add a ``register_reviewed`` call here in the same change that adds
    a handler.
    """
    from lab import chat, loop
    from lab.handlers import git_read, mcp_call, skill_run, web, workspace

    supervisor.register_reviewed(workspace.KIND, workspace.REF, tools=workspace.TOOLS)
    supervisor.register_reviewed(git_read.KIND, git_read.REF, tools=git_read.TOOLS)
    if configure_skill_runner(supervisor):
        supervisor.register_reviewed(skill_run.KIND, skill_run.REF, tools=skill_run.TOOLS)
    servers = signed_mcp_servers(supervisor)
    if servers:
        supervisor.register_reviewed(mcp_call.KIND, mcp_call.REF, tools=mcp_call.TOOLS,
                                     mcp_servers=servers)

    model = loop.model_from_env(supervisor.config.db_path)
    if model is not None:
        loop.register(supervisor, model)
        chat.register(supervisor, model)
        hosts = web_hosts_from_env()
        if hosts:
            supervisor.broker.set_summarizer(loop.evidence_summarizer(model))
            supervisor.register_reviewed(web.KIND, web.REF, tools=web.TOOLS,
                                         egress_hosts=hosts)
