"""Reproduce an accessible H1/H1b SVG solely from the frozen run records.

No model inference or research instrument is invoked. Existing run records and
their SHA-256 digests are required; editing them makes this command refuse.
H1b is exploratory and must never be presented as a rerun of registered H1.

From repository root:
    uv run python scripts/h1_figure.py --check
    uv run python scripts/h1_figure.py --out evals/h1_review/h1-results.svg
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
REVIEW = ROOT / "evals" / "h1_review"
TEMPLATE = REVIEW / "h1-results-template.svg"
SVG = REVIEW / "h1-results.svg"

H1_RECORD = REVIEW / "h1-run.json"
H1B_RECORD = REVIEW / "h1b-run.json"
H1_SHA256 = "9d06bed917dbea2e1d83e29856b820d68c9c9c5cd1f3967363f3f112148b8b3d"
H1B_SHA256 = "e3b4dda4a674407025b7eb06380317578e89ec9a79aa4c868fa4dcfde62e0e9b"
CASES_SHA256 = "f52e8f309733e78b56d1ac0ba354ff1c178bbf15912a196ef4038e3f93e0b396"
RANK = {"no_artifact": 0, "insufficient_evidence": 0, "post": 1, "blog": 2,
        "paper": 3}


def load_record(path: Path, expected_sha256: str) -> dict[str, Any]:
    """Read a frozen record only when its exact committed bytes match."""
    raw = path.read_bytes()
    actual = hashlib.sha256(raw).hexdigest()
    if actual != expected_sha256:
        raise ValueError(f"{path.name}: SHA-256 differs from frozen record")
    record: dict[str, Any] = json.loads(raw)
    if record.get("valid") is not True or record.get("model_errors"):
        raise ValueError(f"{path.name}: invalid run record")
    if record.get("cases_sha256") != CASES_SHA256:
        raise ValueError(f"{path.name}: different case file")
    return record


def summary(record: dict[str, Any]) -> dict[str, Any]:
    """Compute every plotted value from per-case predictions, not report totals."""
    rows = record["report"]["rows"]
    if len(rows) != 30 or len({row["case_id"] for row in rows}) != 30:
        raise ValueError("each run must contain 30 unique cases")
    if any(row["expected"] not in RANK or row["baseline"] not in RANK
           or (row["candidate"] is not None and row["candidate"] not in RANK)
           for row in rows):
        raise ValueError("record contains a route outside the five frozen labels")
    base = sum(row["baseline"] == row["expected"] for row in rows)
    correct = sum(row["candidate"] == row["expected"] for row in rows)
    false_promotions = sum(
        row["candidate"] is not None
        and RANK[row["candidate"]] > RANK[row["expected"]]
        for row in rows
    )
    if (record["report"]["n"] != len(rows)
            or record["report"]["false_promotions"] != false_promotions
            or abs(record["report"]["baseline_accuracy"] - base / 30) > 1e-9
            or abs(record["report"]["candidate_accuracy_overall"] - correct / 30) > 1e-9):
        raise ValueError("per-case data disagree with recorded summary")
    return {"rows": rows, "base": base, "correct": correct,
            "false_promotions": false_promotions}


def render() -> str:
    h1 = summary(load_record(H1_RECORD, H1_SHA256))
    h1b = summary(load_record(H1B_RECORD, H1B_SHA256))
    cases_h1 = {row["case_id"]: (row["expected"], row["baseline"])
                for row in h1["rows"]}
    cases_h1b = {row["case_id"]: (row["expected"], row["baseline"])
                 for row in h1b["rows"]}
    if cases_h1 != cases_h1b:
        raise ValueError("H1 and H1b did not use the same frozen cases and baseline")
    if (h1["base"], h1["correct"], h1["false_promotions"],
            h1b["correct"], h1b["false_promotions"]) != (25, 3, 5, 13, 8):
        raise ValueError("frozen published counts changed; inspect inputs first")

    def label(n: int) -> str:
        return f"{n}/30 ({100 * n / 30:.1f}%)"

    replacements = {
        "RUBRIC_BAR_WIDTH": f"{450 * h1['base'] / 30:.1f}",
        "H1_BAR_WIDTH": f"{450 * h1['correct'] / 30:.1f}",
        "H1B_BAR_WIDTH": f"{450 * h1b['correct'] / 30:.1f}",
        "H1_FALSE_BAR_WIDTH": f"{32 * h1['false_promotions']:.1f}",
        "H1B_FALSE_BAR_WIDTH": f"{32 * h1b['false_promotions']:.1f}",
        "RUBRIC_LABEL": label(h1["base"]),
        "H1_LABEL": label(h1["correct"]),
        "H1B_LABEL": label(h1b["correct"]),
        "H1_FALSE_LABEL": f"{h1['false_promotions']} / 30",
        "H1B_FALSE_LABEL": f"{h1b['false_promotions']} / 30",
        "H1_HASH": H1_SHA256,
        "H1B_HASH": H1B_SHA256,
    }
    image = TEMPLATE.read_text(encoding="utf-8")
    for key, value in replacements.items():
        token = "{{" + key + "}}"
        if image.count(token) != 1:
            raise ValueError(f"figure template needs exactly one {token}")
        image = image.replace(token, value)
    if "{{" in image:
        raise ValueError("unfilled figure template variable")
    return image


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="H1 descriptive SVG from sealed records")
    operation = parser.add_mutually_exclusive_group()
    operation.add_argument("--check", action="store_true",
                           help="verify the committed SVG matches frozen records; never write")
    operation.add_argument("--out", type=Path, metavar="SVG",
                           help="write the regenerated SVG to this path")
    args = parser.parse_args(argv)
    rendered = render()
    if args.check:
        if SVG.read_text(encoding="utf-8") != rendered:
            raise SystemExit("H1 SVG differs from frozen records; regenerate and inspect")
        print("H1 figure reproducible from frozen records")
        return 0
    if args.out is None:
        parser.error("pass --check or --out SVG")
    args.out.write_text(rendered, encoding="utf-8")
    print(f"Wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
