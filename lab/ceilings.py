"""Measure reviewed handlers to size the per-task ceilings (#180, #16).

``lab measure-ceilings`` runs a fixed set of sample tasks
(``evals/ceilings/tasks.json``) through a real supervisor: every handler
that ``lab.handlers.register_all`` registers in a worker process, plus any
named with ``--handler``. Each task runs in its own worker process, through
the broker and the policy engine, as it would on a running lab. When the
worker finishes it reports its own peak resident memory and CPU seconds
(``getrusage``, ``lab.worker.own_usage``). A worker killed at a ceiling
cannot report, so the breach the supervisor records is reported instead.

Nothing outside the machine is needed. Handlers that use the model get a
mock OpenAI-compatible server on loopback, and handlers that fetch a page
reach a local server through the real egress gateway, with a resolver and a
transport that send every allowed host there.

The suggestion is the largest peak across every handler and sample, times a
stated headroom factor, rounded up. The ceilings are one setting for every
reviewed handler, so the largest peak decides. This module only measures and
suggests: a person changes ``SupervisorConfig`` after reading the report.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import math
import os
import subprocess
import tempfile
import threading
import time
from collections.abc import Iterator
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from lab import evals
from lab.egress import Response, socket_transport
from lab.queue import Task
from lab.supervisor import Supervisor, SupervisorConfig
from lab.worker import check_reference

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_TASKS = ROOT / "evals" / "ceilings" / "tasks.json"
DEFAULT_OUT = ROOT / "evals" / "ceilings"
DEFAULT_HEADROOM = 2.0
DEFAULT_REPEATS = 5
# A public address the gateway accepts. Nothing connects to it: the
# measurement transport sends every connection to the local page server.
STAND_IN_ADDRESS = "93.184.216.34"
MOCK_MODEL = "ceilings-mock-model"
MOCK_REVISION = "0" * 40

METHOD = ("each sample task runs in its own worker process through a real supervisor, broker "
          "and policy engine; peak RSS and CPU seconds are the worker's own getrusage "
          "(ru_maxrss, ru_utime + ru_stime, self and children) when it finishes; the model and "
          "web pages are local fakes, so model inference is not in these figures (the model "
          "server is a separate process the ceilings do not cover)")


class CeilingsError(ValueError):
    pass


@dataclass
class Run:
    sample: str
    repeat: int
    state: str
    peak_rss_mb: float | None = None
    cpu_seconds: float | None = None
    wall_seconds: float | None = None
    error: str | None = None
    exceeded: dict[str, Any] | None = None


@dataclass
class HandlerReport:
    ref: str
    source: str                        # "register_all" or "--handler"
    samples: list[str] = field(default_factory=list)
    runs: list[Run] = field(default_factory=list)
    peak_rss_mb: float | None = None
    peak_cpu_seconds: float | None = None
    suggested_rss_mb: int | None = None
    suggested_cpu_seconds: int | None = None
    problems: list[str] = field(default_factory=list)


@dataclass
class CeilingsReport:
    started_at: str
    tasks_file: str
    tasks_sha256: str
    repeats: int
    headroom: float
    measured_under: dict[str, Any]
    current_defaults: dict[str, Any]
    method: str = METHOD
    handlers: dict[str, HandlerReport] = field(default_factory=dict)
    suggested: dict[str, Any] = field(default_factory=dict)
    problems: list[str] = field(default_factory=list)
    provenance: dict[str, Any] = field(default_factory=dict)


def suggest(peak: float, headroom: float, *, floor: int = 1) -> int:
    """``peak`` times ``headroom``, rounded up to a whole unit, at least ``floor``."""
    return max(floor, math.ceil(peak * headroom))


# ----------------------------------------------------------- the task file


def load_tasks(path: Path) -> dict[str, Any]:
    data: Any = json.loads(Path(path).read_text(encoding="utf-8"))
    handlers = data.get("handlers") if isinstance(data, dict) else None
    if not isinstance(handlers, dict):
        raise CeilingsError(f"{path}: needs a 'handlers' object")
    for kind, entry in handlers.items():
        samples = entry.get("samples") if isinstance(entry, dict) else None
        if not isinstance(samples, list) or not samples:
            raise CeilingsError(f"{path}: handler {kind!r} needs a non-empty 'samples' list")
        names = [s.get("name") if isinstance(s, dict) else None for s in samples]
        if not all(isinstance(n, str) and n for n in names) or len(set(names)) != len(names):
            raise CeilingsError(f"{path}: every sample of {kind!r} needs a unique 'name'")
        for sample in samples:
            if not isinstance(sample.get("payload"), dict) \
                    or not isinstance(sample.get("workspace", {}), dict):
                raise CeilingsError(f"{path}: sample {kind}/{sample['name']} needs a payload "
                                    "object, and 'workspace' must be an object")
    spec: dict[str, Any] = data
    return spec


def seed_workspace(root: Path, setup: dict[str, Any]) -> None:
    """Put a sample's files in place before its worker starts."""
    for rel, content in (setup.get("files") or {}).items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(str(content), encoding="utf-8")
    generate = setup.get("generate")
    if generate:
        folder = root / str(generate.get("dir", "generated"))
        folder.mkdir(parents=True, exist_ok=True)
        size = int(generate.get("bytes", 1024))
        for n in range(int(generate.get("count", 10))):
            line = f"line {n}: TODO check this value\n" if n % 7 == 0 else f"line {n}: ok\n"
            (folder / f"file{n:04d}.txt").write_text((line * (size // len(line) + 1))[:size],
                                                       encoding="utf-8")
    repo = setup.get("git")
    if repo:
        _git_repository(root / str(repo.get("dir", "repo")), int(repo.get("commits", 5)),
                        int(repo.get("lines", 50)))


def _git_repository(path: Path, commits: int, lines: int) -> None:
    path.mkdir(parents=True, exist_ok=True)
    env = {"PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin", "HOME": str(path),
           "GIT_CONFIG_NOSYSTEM": "1", "GIT_AUTHOR_NAME": "lab", "GIT_AUTHOR_EMAIL": "lab@local",
           "GIT_COMMITTER_NAME": "lab", "GIT_COMMITTER_EMAIL": "lab@local", "LANG": "C"}

    def git(*args: str) -> None:
        # A fixed argv built here, no shell.
        subprocess.run(["git", *args], cwd=path, env=env, check=True, capture_output=True,
                       timeout=60)

    git("init", "-q")
    for n in range(commits):
        body = "".join(f"commit {n} line {i}\n" for i in range(lines))
        (path / f"module{n % 5}.txt").write_text(body, encoding="utf-8")
        git("add", "-A")
        git("commit", "-q", "-m", f"change {n}")
    # An uncommitted change, so status and diff have something to show.
    (path / "module0.txt").write_text("".join(f"edited line {i}\n" for i in range(lines)),
                                      encoding="utf-8")


# ----------------------------------------------------- the local fakes


class _FakeServer(ThreadingHTTPServer):
    pages: dict[str, str]
    model_reply: str


class _FakeHandler(BaseHTTPRequestHandler):
    server: _FakeServer

    def log_message(self, format: str, *args: Any) -> None:
        pass

    def _send(self, status: int, body: bytes, kind: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        page = self.server.pages.get(self.path.split("?", 1)[0])
        if page is None:
            self._send(404, b"not found", "text/plain")
        else:
            self._send(200, page.encode(), "text/html; charset=utf-8")

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        try:
            request = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            request = {}
        if not self.path.endswith("/chat/completions"):
            self._send(404, b"not found", "text/plain")
            return
        body = {"model": request.get("model", MOCK_MODEL),
                "choices": [{"message": {"role": "assistant",
                                         "content": self.server.model_reply}}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 20}}
        self._send(200, json.dumps(body).encode(), "application/json")


@contextlib.contextmanager
def fake_server(pages: dict[str, str], model_reply: str) -> Iterator[int]:
    server = _FakeServer(("127.0.0.1", 0), _FakeHandler)
    server.pages, server.model_reply = pages, model_reply
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield int(server.server_address[1])
    finally:
        server.shutdown()
        server.server_close()


@contextlib.contextmanager
def measurement_environment(port: int, web_hosts: list[str]) -> Iterator[None]:
    """Point ``register_all`` at the mock model and the sample hosts, then restore."""
    values = {"LAB_MODEL_URL": f"http://127.0.0.1:{port}/v1", "LAB_MODEL_NAME": MOCK_MODEL,
              "LAB_MODEL_REVISION": MOCK_REVISION, "LAB_MODEL_TOKENIZER_REVISION": MOCK_REVISION,
              "LAB_MODEL_WEIGHTS_MB": "1", "LAB_WEB_FETCH_HOSTS": ",".join(web_hosts)}
    saved = {k: os.environ.get(k) for k in values}
    os.environ.update(values)
    try:
        yield
    finally:
        for key, old in saved.items():
            if old is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = old


# ------------------------------------------------------------- measuring


def parse_handler(text: str) -> tuple[str, str]:
    kind, sep, ref = text.partition("=")
    if not sep or not kind:
        raise CeilingsError(f"--handler {text!r}: expected KIND=lab.handlers.module:function")
    check_reference(ref)
    return kind, ref


def measure(tasks_path: Path = DEFAULT_TASKS, *, repeats: int = DEFAULT_REPEATS,
            headroom: float = DEFAULT_HEADROOM, extra: list[tuple[str, str]] | None = None,
            include_registered: bool = True, max_rss_mb: int | None = None,
            max_cpu_seconds: float | None = None,
            task_timeout_seconds: float = 300.0) -> CeilingsReport:
    """Run every sample ``repeats`` times in real workers and build the report.

    ``max_rss_mb`` and ``max_cpu_seconds`` are the ceilings the measurement
    itself runs under; unset, they are the current defaults, so a runaway
    handler is killed and reported rather than allowed to take the machine.
    """
    if repeats < 1:
        raise CeilingsError("repeats must be at least 1")
    if headroom < 1:
        raise CeilingsError("headroom must be at least 1")
    tasks_path = Path(tasks_path)
    spec = load_tasks(tasks_path)
    defaults = SupervisorConfig(db_path="unused.db")
    rss_limit = defaults.task_max_rss_mb if max_rss_mb is None else max_rss_mb
    cpu_limit = defaults.task_max_cpu_seconds if max_cpu_seconds is None else max_cpu_seconds
    report = CeilingsReport(
        started_at=datetime.now(UTC).isoformat(timespec="seconds"),
        tasks_file=_display(tasks_path),
        tasks_sha256=hashlib.sha256(tasks_path.read_bytes()).hexdigest(),
        repeats=repeats, headroom=headroom,
        measured_under={"task_max_rss_mb": rss_limit, "task_max_cpu_seconds": cpu_limit,
                        "task_timeout_seconds": task_timeout_seconds},
        current_defaults={"task_max_rss_mb": defaults.task_max_rss_mb,
                          "task_max_cpu_seconds": defaults.task_max_cpu_seconds},
        provenance=evals.collect_provenance())

    web_hosts = [str(h) for h in spec.get("web_hosts", [])]
    pages = {str(k): str(v) for k, v in (spec.get("pages") or {}).items()}
    model_reply = str(spec.get("model_reply", '{"summary": "A page."}'))
    with tempfile.TemporaryDirectory(prefix="lab-ceilings-") as scratch, \
            fake_server(pages, model_reply) as port, \
            measurement_environment(port, web_hosts):
        _measure_in(Path(scratch), spec, report, port, web_hosts, extra or [],
                    include_registered, rss_limit, cpu_limit, task_timeout_seconds)
    _summarize(report)
    return report


def _measure_in(scratch: Path, spec: dict[str, Any], report: CeilingsReport, port: int,
                web_hosts: list[str], extra: list[tuple[str, str]], include_registered: bool,
                rss_limit: int | None, cpu_limit: float | None, timeout: float) -> None:
    def resolve(host: str, _port: int) -> list[str]:
        if host not in web_hosts:
            raise OSError(f"{host} is not a sample host")
        return [STAND_IN_ADDRESS]

    def transport(ip: str, _port: int, host: str, target: str, timeout: float,
                  max_bytes: int, **kw: Any) -> Response:
        return socket_transport("127.0.0.1", port, host, target, timeout, max_bytes,
                                **{**kw, "tls": False})

    config = SupervisorConfig(db_path=scratch / "lab.db", heavy_slots=1, light_slots=1,
                              idle_poll_seconds=0.01, task_timeout_seconds=timeout,
                              task_max_rss_mb=rss_limit, task_max_cpu_seconds=cpu_limit,
                              ceiling_poll_seconds=0.05)
    # The supervisor warns that approvals are not signature-checked. Nothing
    # here asks for an approval, and the warning would only be noise.
    sup_log = logging.getLogger("lab.supervisor")
    level = sup_log.level
    sup_log.setLevel(logging.ERROR)
    sup = Supervisor(config, egress_resolver=resolve, egress_transport=transport)
    try:
        registered = _register(sup, spec, extra, include_registered, report)
        usage: dict[str, dict[str, float]] = {}
        sup.worker_usage = lambda task, measured: usage.__setitem__(task.id, measured)
        plan = _queue_samples(sup, spec, registered, report)
        if plan:
            asyncio.run(sup.run(max_tasks=len(plan)))
        for task_id, (kind, sample, repeat, wall) in plan.items():
            report.handlers[kind].runs.append(
                _run_of(sup, task_id, sample["name"], repeat, usage.get(task_id), wall))
    finally:
        sup.close()
        sup_log.setLevel(level)


def _register(sup: Supervisor, spec: dict[str, Any], extra: list[tuple[str, str]],
              include_registered: bool, report: CeilingsReport) -> list[str]:
    """Register the handlers to measure; return their kinds in order."""
    reviewed: dict[str, str] = {}
    original = sup.register_reviewed

    def recording(kind: str, ref: str, tools: frozenset[str] | set[str] = frozenset(),
                  **kw: Any) -> None:
        original(kind, ref, tools, **kw)
        reviewed[kind] = ref

    if include_registered:
        from lab import handlers
        # Only handlers that run in a worker process are under the ceilings,
        # so only those registered through register_reviewed are measured.
        setattr(sup, "register_reviewed", recording)  # noqa: B010
        try:
            handlers.register_all(sup)
        finally:
            setattr(sup, "register_reviewed", original)  # noqa: B010
        for kind, ref in reviewed.items():
            report.handlers[kind] = HandlerReport(ref, "register_all")
    for kind, ref in extra:
        tools = (spec["handlers"].get(kind) or {}).get("tools", [])
        sup.register_reviewed(kind, ref, tools=frozenset(str(t) for t in tools))
        report.handlers[kind] = HandlerReport(ref, "--handler")
    return list(report.handlers)


def _queue_samples(sup: Supervisor, spec: dict[str, Any], kinds: list[str],
                   report: CeilingsReport) -> dict[str, tuple[str, dict[str, Any], int,
                                                         list[float]]]:
    plan: dict[str, tuple[str, dict[str, Any], int, list[float]]] = {}
    for kind in kinds:
        entry = spec["handlers"].get(kind)
        if entry is None:
            report.handlers[kind].problems.append(
                f"no sample tasks for {kind!r} in the task file; add some")
            continue
        report.handlers[kind].samples = [s["name"] for s in entry["samples"]]
        original = sup._handlers[kind]

        async def seeded(task: Task, session: Any, _original: Any = original) -> dict[str, Any]:
            _, sample, _, wall = plan[task.id]
            seed_workspace(sup.broker._workspace_for(task.id).root,
                           sample.get("workspace") or {})
            began = time.monotonic()
            try:
                result: dict[str, Any] = await _original(task, session)
                return result
            finally:
                wall.append(time.monotonic() - began)

        sup._handlers[kind] = seeded
        for repeat in range(1, report.repeats + 1):
            for sample in entry["samples"]:
                task_id = sup.queue.add_task(
                    f"ceilings {kind} {sample['name']} #{repeat}", payload=sample["payload"],
                    agent_kind=kind, max_attempts=1, idempotent=True)
                plan[task_id] = (kind, sample, repeat, [])
    return plan


def _run_of(sup: Supervisor, task_id: str, name: str, repeat: int,
            usage: dict[str, float] | None, wall: list[float]) -> Run:
    task = sup.queue.get(task_id)
    state = task.state if task else "missing"
    run = Run(name, repeat, state, wall_seconds=round(wall[0], 3) if wall else None,
              error=None if state == "succeeded" or task is None else task.last_error)
    if usage is not None:
        run.peak_rss_mb = round(usage["peak_rss_mb"], 1)
        run.cpu_seconds = round(usage["cpu_seconds"], 3)
    for row in sup.queue.events(task_id):
        if row["kind"] == "resource_ceiling_exceeded":
            run.exceeded = json.loads(row["detail"])
    return run


def _summarize(report: CeilingsReport) -> None:
    all_rss: list[float] = []
    all_cpu: list[float] = []
    for kind, handler in report.handlers.items():
        for run in handler.runs:
            if run.exceeded:
                handler.problems.append(
                    f"{run.sample} #{run.repeat} went over the {run.exceeded['resource']} "
                    "ceiling it was measured under; rerun with a higher ceiling")
            elif run.state != "succeeded":
                handler.problems.append(f"{run.sample} #{run.repeat} ended {run.state}: "
                                        f"{run.error}")
            elif run.peak_rss_mb is None or run.cpu_seconds is None:
                handler.problems.append(f"{run.sample} #{run.repeat} reported no usage")
        measured = [r for r in handler.runs if r.peak_rss_mb is not None]
        if measured:
            handler.peak_rss_mb = max(r.peak_rss_mb or 0.0 for r in measured)
            handler.peak_cpu_seconds = max(r.cpu_seconds or 0.0 for r in measured)
        if handler.problems or not measured:
            # A peak from an incomplete run set understates the real one.
            if not handler.problems:
                handler.problems.append("nothing was measured")
            report.problems.extend(f"{kind}: {p}" for p in handler.problems)
            continue
        assert handler.peak_rss_mb is not None and handler.peak_cpu_seconds is not None
        handler.suggested_rss_mb = suggest(handler.peak_rss_mb, report.headroom)
        handler.suggested_cpu_seconds = suggest(handler.peak_cpu_seconds, report.headroom)
        all_rss.append(handler.peak_rss_mb)
        all_cpu.append(handler.peak_cpu_seconds)
    if not report.handlers:
        report.problems.append("no reviewed handler is registered; nothing was measured")
    complete = not report.problems
    report.suggested = {
        "task_max_rss_mb": suggest(max(all_rss), report.headroom) if complete else None,
        "task_max_cpu_seconds": suggest(max(all_cpu), report.headroom) if complete else None,
        "basis": (f"largest peak over every handler and sample, times {report.headroom:g}, "
                  "rounded up" if complete else
                  "none: fix the problems listed and measure again"),
    }


# -------------------------------------------------------------- output


def _display(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(ROOT))
    except ValueError:
        return str(path)


def _fmt(value: float | None, spec: str) -> str:
    return "-" if value is None else format(value, spec)


def render(report: CeilingsReport) -> str:
    lines = [f"{'handler':<20} {'runs':>5} {'peak MB':>9} {'peak cpu s':>11} "
             f"{'suggest MB':>11} {'suggest s':>10}"]
    for kind, h in report.handlers.items():
        lines.append(f"{kind:<20} {len(h.runs):>5} {_fmt(h.peak_rss_mb, '.1f'):>9} "
                     f"{_fmt(h.peak_cpu_seconds, '.3f'):>11} "
                     f"{_fmt(h.suggested_rss_mb, 'd'):>11} "
                     f"{_fmt(h.suggested_cpu_seconds, 'd'):>10}")
    s, now = report.suggested, report.current_defaults
    lines.append(f"headroom {report.headroom:g}x, {report.repeats} repeats per sample, "
                 f"on {report.provenance.get('os')} {report.provenance.get('machine')}")
    if s.get("task_max_rss_mb") is not None:
        lines.append(f"suggested: task_max_rss_mb={s['task_max_rss_mb']} "
                     f"task_max_cpu_seconds={s['task_max_cpu_seconds']} "
                     f"(defaults now {now['task_max_rss_mb']} MB, "
                     f"{now['task_max_cpu_seconds']:g} s)")
    else:
        lines.append("suggested: none")
    lines.extend(f"problem: {p}" for p in report.problems)
    return "\n".join(lines)


def save(report: CeilingsReport, out_dir: Path) -> Path:
    body = asdict(report)
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"))
    body["sha256"] = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = report.started_at.replace(":", "").replace("-", "")
    path = out_dir / f"ceilings-{stamp}-{body['sha256'][:8]}.json"
    path.write_text(json.dumps(body, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path
