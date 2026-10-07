"""The final H1 labels from the three AI reviewers' replies (#84, amendment 2).

The rule is the one registered in ``docs/PREREGISTRATION-AMENDMENT-2-DRAFT.md``
(osf.io/q75bx), applied mechanically:

* A case takes the route that at least two of the three reviewers chose.
* A main case on which all three differ leaves the set. Split main cases are
  replaced in ascending order of case number, each by the lowest-numbered unused
  spare. A spare takes its label by the same rule; a spare on which all three
  differ is skipped and reported, and the next spare is tried.
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
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from lab.reviewsheet import ROUTES, ReviewError, blind, load_cases, render_sheet

REVIEWERS = 3


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
                 spare_replies: dict[str, object]) -> dict[str, Any]:
    """Apply the registered rule. ``main`` and ``spares`` are ``blind`` output, in id order."""
    main_ids, spare_ids = [n for n, _ in main], [n for n, _ in spares]
    if set(main_replies) != set(spare_replies):
        raise ReviewError("the main and spare replies must come from the same reviewers")
    main_votes = _votes(main_replies, main_ids)
    spare_votes = _votes(spare_replies, spare_ids)
    by_id = dict(main) | dict(spares)

    kept: list[tuple[str, str]] = []
    split: list[str] = []
    for neutral in sorted(main_ids):
        route = _majority(list(main_votes[neutral].values()))
        if route is None:
            split.append(neutral)
        else:
            kept.append((neutral, route))

    remaining = sorted(spare_ids)
    replacements: list[dict[str, Any]] = []
    skipped: list[str] = []
    unreplaced: list[str] = []
    for neutral in split:
        while remaining:
            spare = remaining.pop(0)
            route = _majority(list(spare_votes[spare].values()))
            if route is None:
                skipped.append(spare)
                continue
            replacements.append({"case": neutral, "id": by_id[neutral]["id"],
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
        "testable": not unreplaced and len(cases) == len(main),
        "main": _agreement(main_votes),
        "spares": _agreement(spare_votes),
        "replaced": replacements,
        "spares_skipped": skipped,
        "spares_unused": remaining,
        "split_not_replaced": unreplaced,
        "counts": dict(sorted(Counter(r for _, r in kept).items())),
    }


def _blinded(files: Sequence[Path], exclude: Sequence[str], prefix: str,
             sheet: Path) -> list[tuple[str, dict[str, Any]]]:
    cases = [c for path in files for c in load_cases(path, labeled=False)]
    blinded = blind(cases, exclude=exclude, prefix=prefix)
    if render_sheet(blinded) + "\n" != sheet.read_text(encoding="utf-8"):
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
        out[name] = json.loads(Path(path).read_text(encoding="utf-8"))
    return out


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
        if args.out.exists() or args.report.exists():
            raise ReviewError("the output exists; final labels are written once")
        main_cases = _blinded(args.cases, args.exclude, "case", args.sheet)
        spare_cases = _blinded([args.spares], [], "spare", args.spare_sheet)
        result = final_labels(main_cases, spare_cases, _replies(args.answers),
                              _replies(args.spare_answers))
        text = "".join(json.dumps(c, ensure_ascii=False) + "\n" for c in result.pop("cases"))
        args.out.write_text(text, encoding="utf-8")
        result["file"] = str(args.out)
        result["sha256"] = hashlib.sha256(text.encode("utf-8")).hexdigest()
        args.report.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    except (ReviewError, OSError, ValueError) as exc:
        print(f"h1_labels: {exc}", file=sys.stderr)
        return 1
    print(f"wrote {len(result['labels'])} cases to {args.out} (sha256 {result['sha256']})")
    if not result["testable"]:
        print("H1 is not testable at the registered size: the spares ran out", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
