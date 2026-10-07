"""The final H1 labels from the three AI reviewers' replies (#84, amendment 2).

The rule is the one registered in ``docs/PREREGISTRATION-AMENDMENT-2-DRAFT.md``
(osf.io/q75bx), applied mechanically:

* A case takes the route that at least two of the three reviewers chose.
* A main case on which all three differ leaves the set. Split main cases are
  replaced in ascending order of case number, each by the lowest-numbered unused
  spare. A spare takes its label by the same rule; a spare on which all three
  differ is skipped and reported, and the next spare is tried.
* A case the ledger cannot build (``lab shadow`` would stop on it) is handled
  the same way as a split: a main case leaves the set and is replaced in the
  same order, and such a spare is skipped. Departure from the registration,
  decided by the owner on 2026-10-07 before any model run (#84).
* If the spares run out, H1 is not testable at the registered size.
* A reply that is missing or malformed stops the run: no label is made from
  fewer than three reviewers.

The neutral ids the reviewers saw are rebuilt with ``reviewsheet.blind`` and the
rebuilt sheets must equal the frozen ones byte for byte, so a label cannot be
attached to the wrong case. The owner's labels are not read here: they are a
separate check, compared only after the final labels are fixed. No model is
called.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import Counter
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from lab.reviewsheet import ROUTES, ReviewError, blind, load_cases, render_sheet

REVIEWERS = 3
REGISTERED_SIZE = 30


def _majority(labels: Sequence[str]) -> str | None:
    """The route at least two reviewers chose, or None when all three differ."""
    route, count = Counter(labels).most_common(1)[0]
    return route if count >= 2 else None


def _check_answers(name: str, answers: object, ids: Sequence[str]) -> dict[str, str]:
    if not isinstance(answers, dict):
        raise ReviewError(f"{name}: the reply must be a JSON object of case id to route")
    extra, missing = set(answers) - set(ids), set(ids) - set(answers)
    if extra:
        raise ReviewError(f"{name}: answers for unknown cases: {sorted(extra)}")
    if missing:
        raise ReviewError(f"{name}: no answer for: {sorted(missing)}")
    bad = {k: v for k, v in answers.items() if v not in ROUTES}
    if bad:
        raise ReviewError(f"{name}: not a route name: {bad}")
    return {str(k): str(v) for k, v in answers.items()}


def _votes(replies: dict[str, object], ids: Sequence[str]) -> dict[str, dict[str, str]]:
    """Case id to {reviewer: route}, after every reply is checked."""
    if len(replies) != REVIEWERS:
        raise ReviewError(f"need replies from exactly {REVIEWERS} reviewers, got "
                          f"{len(replies)}: no label is made from fewer")
    checked = {name: _check_answers(name, answers, ids) for name, answers in replies.items()}
    return {cid: {name: checked[name][cid] for name in checked} for cid in ids}


def _agreement(votes: dict[str, dict[str, str]]) -> dict[str, Any]:
    kinds = Counter(len(set(v.values())) for v in votes.values())
    outvoted: Counter[str] = Counter()
    for vote in votes.values():
        route = _majority(list(vote.values()))
        if route is not None:
            outvoted.update(name for name, label in vote.items() if label != route)
    return {"unanimous": kinds[1], "two_to_one": kinds[2], "three_way": kinds[3],
            "outvoted": {name: outvoted[name] for name in sorted(next(iter(votes.values())))}}


def final_labels(main: list[tuple[str, dict[str, Any]]], spares: list[tuple[str, dict[str, Any]]],
                 main_replies: dict[str, object],
                 spare_replies: dict[str, object], size: int | None = None,
                 builds: Callable[[dict[str, Any]], bool] | None = None) -> dict[str, Any]:
    """Apply the registered rule. ``main`` and ``spares`` are ``blind`` output, in id order.
    ``size`` is the number of cases H1 needs, the registered 30 unless a test sets it.
    ``builds`` says whether the ledger can build a case; the default asks the ledger itself."""
    size = REGISTERED_SIZE if size is None else size
    main_ids, spare_ids = [n for n, _ in main], [n for n, _ in spares]
    if set(main_replies) != set(spare_replies):
        raise ReviewError("the main and spare replies must come from the same reviewers")
    main_votes = _votes(main_replies, main_ids)
    spare_votes = _votes(spare_replies, spare_ids)
    by_id = dict(main) | dict(spares)
    check = ledger_builds if builds is None else builds
    unbuildable = {n for n in [*main_ids, *spare_ids] if not check(by_id[n])}

    kept: list[tuple[str, str]] = []
    leaving: list[tuple[str, str]] = []
    for neutral in sorted(main_ids):
        route = _majority(list(main_votes[neutral].values()))
        if route is None:
            leaving.append((neutral, "three-way split"))
        elif neutral in unbuildable:
            leaving.append((neutral, "cannot be built"))
        else:
            kept.append((neutral, route))

    remaining = sorted(spare_ids)
    replacements: list[dict[str, Any]] = []
    skipped: list[dict[str, str]] = []
    unreplaced: list[str] = []
    for neutral, reason in leaving:
        while remaining:
            spare = remaining.pop(0)
            route = _majority(list(spare_votes[spare].values()))
            if route is None or spare in unbuildable:
                skipped.append({"spare": spare, "reason": "three-way split" if route is None
                                else "cannot be built"})
                continue
            replacements.append({"case": neutral, "id": by_id[neutral]["id"], "reason": reason,
                                 "votes": main_votes[neutral], "spare": spare,
                                 "spare_id": by_id[spare]["id"], "route": route})
            kept.append((spare, route))
            break
        else:
            unreplaced.append(neutral)

    cases = []
    for neutral, route in kept:
        case = dict(by_id[neutral])
        case["expected"] = route
        cases.append(case)
    return {
        "cases": cases,
        "labels": {by_id[n]["id"]: route for n, route in kept},
        "testable": not unreplaced and len(cases) >= size,
        "main": _agreement(main_votes),
        "spares": _agreement(spare_votes),
        "unbuildable": sorted(unbuildable),
        "replaced": replacements,
        "spares_skipped": skipped,
        "spares_unused": remaining,
        "not_replaced": unreplaced,
        "counts": dict(sorted(Counter(r for _, r in kept).items())),
    }


def ledger_builds(case: dict[str, Any]) -> bool:
    """Whether ``lab shadow`` can build this case in a ledger (its route is not used)."""
    import tempfile

    from lab import shadow
    from lab.ledger import LedgerError
    try:
        parsed = shadow.parse_case({**case, "expected": "post"})
        with tempfile.TemporaryDirectory() as tmp:
            shadow.baseline_routes([parsed], Path(tmp))
    except (LedgerError, shadow.ShadowError):
        return False
    return True


def _blinded(files: Sequence[Path], exclude: Sequence[str], prefix: str,
             sheet: Path) -> list[tuple[str, dict[str, Any]]]:
    cases = [c for path in files for c in load_cases(path, labeled=False)]
    ids = [c["id"] for c in cases]
    if len(set(ids)) != len(ids):
        raise ReviewError("the case files share a case id")
    blinded = blind(cases, exclude=exclude, prefix=prefix)
    if (render_sheet(blinded) + "\n").encode("utf-8") != sheet.read_bytes():
        raise ReviewError(f"{sheet}: the rebuilt sheet differs from this one, so the neutral "
                          "ids may not name the cases the reviewers saw")
    return blinded


def _replies(pairs: Sequence[str]) -> dict[str, object]:
    out: dict[str, object] = {}
    for pair in pairs:
        name, sep, path = pair.partition("=")
        if not (sep and name and path):
            raise ReviewError(f"{pair!r}: give a reply as NAME=PATH")
        if name in out:
            raise ReviewError(f"reviewer {name} is given twice")
        out[name] = json.loads(Path(path).read_text(encoding="utf-8"),
                               object_pairs_hook=_no_repeats(name))
    return out


def _no_repeats(name: str) -> Any:
    """A JSON object hook that refuses a reply naming one case twice."""
    def hook(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        keys = [k for k, _ in pairs]
        repeated = sorted({k for k in keys if keys.count(k) > 1})
        if repeated:
            raise ReviewError(f"{name}: a case is answered more than once: {repeated}")
        return dict(pairs)
    return hook


def _write_once(files: list[tuple[Path, bytes]]) -> None:
    """Create every file or none: each is opened exclusively, so a second run or a file that
    already exists stops it, and whatever this run created is removed if a later write fails."""
    made: list[Path] = []
    try:
        for path, data in files:
            with path.open("xb") as handle:
                made.append(path)
                handle.write(data)
    except BaseException:
        for path in made:
            path.unlink(missing_ok=True)
        raise


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="lab.h1_labels", description=__doc__.split("\n")[0])
    parser.add_argument("--cases", type=Path, nargs="+", required=True)
    parser.add_argument("--exclude", nargs="*", default=[], metavar="ID")
    parser.add_argument("--sheet", type=Path, required=True,
                        help="the frozen main sheet the reviewers saw")
    parser.add_argument("--spares", type=Path, required=True)
    parser.add_argument("--spare-sheet", type=Path, required=True)
    parser.add_argument("--answers", nargs="+", required=True, metavar="NAME=PATH")
    parser.add_argument("--spare-answers", nargs="+", required=True, metavar="NAME=PATH")
    parser.add_argument("--out", type=Path, required=True, help="the final labelled case file")
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.out.resolve() == args.report.resolve():
            raise ReviewError("--out and --report must be different files")
        main_cases = _blinded(args.cases, args.exclude, "case", args.sheet)
        spare_cases = _blinded([args.spares], [], "spare", args.spare_sheet)
        result = final_labels(main_cases, spare_cases, _replies(args.answers),
                              _replies(args.spare_answers))
        data = "".join(json.dumps(c, ensure_ascii=False) + "\n"
                       for c in result.pop("cases")).encode("utf-8")
        # Below the registered size the remaining cases are exploratory only, so they are never
        # written where the final file belongs.
        out = args.out if result["testable"] else args.out.with_name(
            f"{args.out.stem}.EXPLORATORY{args.out.suffix}")
        result["file"] = str(out)
        result["sha256"] = hashlib.sha256(data).hexdigest()
        report = (json.dumps(result, indent=2) + "\n").encode("utf-8")
        _write_once([(out, data), (args.report, report)])
    except FileExistsError as exc:
        print(f"h1_labels: {exc.filename} exists; final labels are written once", file=sys.stderr)
        return 1
    except (ReviewError, OSError, ValueError) as exc:
        print(f"h1_labels: {exc}", file=sys.stderr)
        return 1
    print(f"wrote {len(result['labels'])} cases to {out} (sha256 {result['sha256']})")
    if not result["testable"]:
        print("H1 is not testable at the registered size: the spares ran out; the remaining "
              "cases are exploratory only", file=sys.stderr)
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
