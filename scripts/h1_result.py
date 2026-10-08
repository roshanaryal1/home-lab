"""The H1 comparisons the amendment reports with the result (#84).

Reads only committed files: the final case file, the run record, the reviewers'
replies, the owner's labels and the drafts. Prints the numbers in
docs/PREREGISTRATION.md, Results, H1. Run from the repository root:

    uv run python scripts/h1_result.py

With --record PATH it prints only the paired bootstrap interval for another run
record over the same cases, headed EXPLORATORY (H1b, #321). It changes no verdict:

    uv run python scripts/h1_result.py --record evals/h1_review/h1b-run.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
from collections import Counter
from pathlib import Path

from lab import reviewsheet as rs

REVIEW = Path("evals/h1_review")
REPLIES = REVIEW / "ai-replies"
RANK = {"no_artifact": 0, "insufficient_evidence": 0, "post": 1, "blog": 2, "paper": 3}
SEED = 20260930
RESAMPLES = 10_000
LOW, HIGH = 249, 9749  # the 2.5th and 97.5th percentile positions of the sorted resamples


def final_labels() -> dict[str, str]:
    final = {}
    for line in (REVIEW / "final-cases-v2.jsonl").read_text().splitlines():
        case = json.loads(line)
        final[case["id"]] = case["expected"]
    return final


def paired_gain(run: dict, final: dict[str, str]) -> tuple[float, float, float]:
    """The candidate's accuracy gain over the rubric, and its 95% paired bootstrap interval."""
    rows = {r["case_id"]: r for r in run["report"]["rows"]}
    diff = [int(rows[i]["candidate"] == final[i]) - int(rows[i]["baseline"] == final[i])
            for i in final]
    rng = random.Random(SEED)
    boot = sorted(sum(diff[rng.randrange(len(diff))] for _ in diff) / len(diff)
                  for _ in range(RESAMPLES))
    return sum(diff) / len(diff), boot[LOW], boot[HIGH]


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def exploratory(record: Path) -> list[str]:
    """Lines for an exploratory interval on another run record. Not a registered result."""
    final = final_labels()
    run = json.loads(record.read_text())
    registered = json.loads((REVIEW / "h1-run.json").read_text())
    if run.get("cases_sha256") != registered["cases_sha256"]:
        raise SystemExit(f"{record}: its cases are not the registered H1 cases")
    ids = [r["case_id"] for r in run["report"]["rows"]]
    if sorted(ids) != sorted(final):
        raise SystemExit(f"{record}: it must have exactly one row for each final case")
    rows = {r["case_id"]: r for r in run["report"]["rows"]}
    gain, low, high = paired_gain(run, final)
    n = len(final)
    cand = sum(rows[i]["candidate"] == final[i] for i in final)
    base = sum(rows[i]["baseline"] == final[i] for i in final)
    cases = REVIEW / "final-cases-v2.jsonl"
    return [
        "EXPLORATORY. Not a registered result, and it changes no verdict.",
        f"record: {record}",
        f"record SHA-256: {sha256(record)}",
        f"cases: {cases}, {n} cases, SHA-256 {sha256(cases)}",
        "method: paired bootstrap of the per-case difference, candidate correct minus rubric "
        "correct (an abstention counts as a miss). Cases are resampled with replacement, and the "
        "interval is the 2.5th and 97.5th percentile of the resampled gains.",
        f"resamples: {RESAMPLES:,}, seed: {SEED}",
        f"accuracy, candidate: {cand} of {n} ({cand / n:.3f})",
        f"accuracy, rubric: {base} of {n} ({base / n:.3f})",
        f"accuracy gain {gain:+.3f}, 95% CI [{low:+.3f}, {high:+.3f}]",
    ]


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="The H1 comparisons, or an exploratory interval.")
    parser.add_argument("--record", type=Path,
                        help="print only the paired interval for this run record, as EXPLORATORY")
    args = parser.parse_args(argv)
    if args.record is not None:
        print("\n".join(exploratory(args.record)))
        return
    main_cases = (rs.load_cases(Path("evals/shadow_cases_DRAFT.jsonl"))
                  + rs.load_cases(REVIEW / "extra-cases-UNLABELED.jsonl", labeled=False))
    blinded = rs.blind(main_cases, exclude=["d-paper-one-source", "d-paper-control-contradicts"])
    spares = rs.blind(rs.load_cases(REVIEW / "spare-cases-UNLABELED.jsonl", labeled=False),
                      prefix="spare")
    neutral = {c["id"]: n for n, c in blinded + spares}
    drafted = {c["id"]: c.get("expected") for c in main_cases}
    final = final_labels()
    owner = (json.loads((REVIEW / "answers-owner.json").read_text())
             | json.loads((REVIEW / "answers-owner-spares.json").read_text()))
    run = json.loads((REVIEW / "h1-run.json").read_text())
    rows = {r["case_id"]: r for r in run["report"]["rows"]}
    ids = list(final)

    def show(name: str, pairs: list[tuple[str, str]]) -> None:
        same = sum(a == b for a, b in pairs)
        swaps = Counter(f"{a} -> {b}" for a, b in pairs if a != b)
        print(f"{name}: {same} of {len(pairs)}; differ: {dict(sorted(swaps.items()))}")

    show("owner -> final", [(owner[neutral[i]], final[i]) for i in ids])
    show("drafted -> final", [(drafted[i], final[i]) for i in ids if drafted.get(i)])
    show("rubric -> final", [(rows[i]["baseline"], final[i]) for i in ids])
    show("rubric -> owner", [(rows[i]["baseline"], owner[neutral[i]]) for i in ids])
    show("candidate -> owner", [(str(rows[i]["candidate"]), owner[neutral[i]]) for i in ids])
    print("candidate routes:", dict(Counter(str(rows[i]["candidate"]) for i in ids)))
    print("per class (final label): cases, rubric right, candidate right, candidate abstained")
    for route in ("no_artifact", "insufficient_evidence", "post", "blog", "paper"):
        of = [i for i in ids if final[i] == route]
        print(f"  {route}: {len(of)}, {sum(rows[i]['baseline'] == route for i in of)}, "
              f"{sum(rows[i]['candidate'] == route for i in of)}, "
              f"{sum(rows[i]['candidate'] is None for i in of)}")
    for i in ids:
        cand = rows[i]["candidate"]
        if cand is not None and RANK[cand] > RANK[final[i]]:
            print(f"false promotion: {neutral[i]} {i} final {final[i]}, owner "
                  f"{owner[neutral[i]]}, candidate {cand}")
    votes = [json.loads((REPLIES / f).read_text()) for f in (
        "answers-gpt-5.6-sol-1.json", "answers-gemini-3.6-flash-1.json",
        "answers-deepseek-flash-3.json")]
    print(f"replaced case-14 (x-home-readable): AI votes {[v['case-14'] for v in votes]}, "
          f"owner {owner['case-14']}")
    gain, low, high = paired_gain(run, final)
    print(f"accuracy gain {gain:+.3f}, 95% CI "
          f"[{low:+.3f}, {high:+.3f}] (paired bootstrap, seed {SEED})")


if __name__ == "__main__":
    main()
