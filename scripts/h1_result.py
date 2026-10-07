"""The H1 comparisons the amendment reports with the result (#84).

Reads only committed files: the final case file, the run record, the reviewers'
replies, the owner's labels and the drafts. Prints the numbers in
docs/PREREGISTRATION.md, Results, H1. Run from the repository root:

    uv run python scripts/h1_result.py
"""

from __future__ import annotations

import json
import random
from collections import Counter
from pathlib import Path

from lab import reviewsheet as rs

REVIEW = Path("evals/h1_review")
REPLIES = REVIEW / "ai-replies"
RANK = {"no_artifact": 0, "insufficient_evidence": 0, "post": 1, "blog": 2, "paper": 3}


def main() -> None:
    main_cases = (rs.load_cases(Path("evals/shadow_cases_DRAFT.jsonl"))
                  + rs.load_cases(REVIEW / "extra-cases-UNLABELED.jsonl", labeled=False))
    blinded = rs.blind(main_cases, exclude=["d-paper-one-source", "d-paper-control-contradicts"])
    spares = rs.blind(rs.load_cases(REVIEW / "spare-cases-UNLABELED.jsonl", labeled=False),
                      prefix="spare")
    neutral = {c["id"]: n for n, c in blinded + spares}
    drafted = {c["id"]: c.get("expected") for c in main_cases}
    final = {}
    for line in (REVIEW / "final-cases-v2.jsonl").read_text().splitlines():
        case = json.loads(line)
        final[case["id"]] = case["expected"]
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
    diff = [int(rows[i]["candidate"] == final[i]) - int(rows[i]["baseline"] == final[i])
            for i in ids]
    rng = random.Random(20260930)
    boot = sorted(sum(diff[rng.randrange(len(diff))] for _ in diff) / len(diff)
                  for _ in range(10_000))
    print(f"accuracy gain {sum(diff) / len(diff):+.3f}, 95% CI "
          f"[{boot[249]:+.3f}, {boot[9749]:+.3f}] (paired bootstrap, seed 20260930)")


if __name__ == "__main__":
    main()
