"""A blinded review sheet for the H1 case labels (#84).

H1 needs shadow cases whose labels do not simply repeat the rubric. The
drafts were labeled by someone who had read the rubric, so an independent
person has to label them first. This module makes that possible without
leaking the answer:

* ``sheet`` writes a markdown sheet and an answers template. Cases get neutral
  ids in a seeded shuffle, the drafted labels and original ids are left out,
  and the routes are described in plain words, without the rubric's counts.
* ``compare`` recomputes the same shuffle from the same seed and lists where
  the reviewer's labels differ from the drafted ones.

Nothing here decides what to do with a disagreement; that is a pre-registration
decision for the owner (see ``evals/h1_review/README.md``). No model is called.
"""

from __future__ import annotations

import argparse
import json
import random
import re
import sys
from collections import Counter
from collections.abc import Sequence
from pathlib import Path
from typing import Any

ROUTES = ("post", "blog", "paper", "insufficient_evidence", "no_artifact")

# Plain meanings only. The rubric's own thresholds (how many sources, which
# evidence types) are left out on purpose: a reviewer who applies them just
# reproduces the rubric, and then H1 cannot show a gain over it.
MEANINGS = {
    "post": "One incident, commit or lesson worth a short note. At least one claim is "
            "backed by evidence that supports it.",
    "blog": "A pattern seen in more than one incident, together with an explanation of "
            "why it happens.",
    "paper": "A measurement that holds up when it is checked against a baseline and a "
             "control.",
    "insufficient_evidence": "There are claims, but not enough behind them for even a "
                             "short post.",
    "no_artifact": "There is nothing to say: no claims at all.",
}

DEFAULT_SEED = 20260930


class ReviewError(ValueError):
    """The cases or the answers cannot be used."""


def _check_claims(claims: list[Any], where: str) -> None:
    """Every claim and evidence item must have what the sheet prints, as text."""
    for n, claim in enumerate(claims, 1):
        if not (isinstance(claim, dict) and isinstance(claim.get("text"), str)):
            raise ReviewError(f"{where}: claim {n} needs a text")
        if not isinstance(claim.get("kind", "finding"), str):
            raise ReviewError(f"{where}: claim {n}: kind must be text")
        if not isinstance(claim.get("verified", False), bool):
            raise ReviewError(f"{where}: claim {n}: verified must be true or false")
        evidence = claim.get("evidence", [])
        if not isinstance(evidence, list):
            raise ReviewError(f"{where}: claim {n}: evidence must be a list")
        for m, item in enumerate(evidence, 1):
            if not (isinstance(item, dict)
                    and all(isinstance(item.get(k), str) for k in ("source", "type", "text"))):
                raise ReviewError(f"{where}: claim {n}, evidence {m} needs source, type, text")
            if item.get("relation", "supports") not in ("supports", "contradicts"):
                raise ReviewError(f"{where}: claim {n}, evidence {m}: relation must be "
                                  "supports or contradicts")


def load_cases(path: Path) -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            case = json.loads(line)
        except ValueError as exc:
            raise ReviewError(f"{path}:{number}: not JSON") from exc
        if not (isinstance(case, dict) and isinstance(case.get("id"), str)
                and case.get("expected") in ROUTES and isinstance(case.get("claims"), list)):
            raise ReviewError(f"{path}:{number}: needs id, a known expected route and claims")
        _check_claims(case["claims"], f"{path}:{number}")
        cases.append(case)
    ids = [c["id"] for c in cases]
    if len(set(ids)) != len(ids):
        raise ReviewError(f"{path}: duplicate case ids")
    return cases


def blind(cases: list[dict[str, Any]], seed: int = DEFAULT_SEED,
          exclude: Sequence[str] = ()) -> list[tuple[str, dict[str, Any]]]:
    """Neutral ids in a seeded shuffle; the same inputs always give the same result."""
    unknown = set(exclude) - {c["id"] for c in cases}
    if unknown:
        raise ReviewError(f"cannot exclude unknown case ids: {sorted(unknown)}")
    kept = [c for c in cases if c["id"] not in set(exclude)]
    order = sorted(kept, key=lambda c: c["id"])
    random.Random(seed).shuffle(order)
    return [(f"case-{i:02d}", c) for i, c in enumerate(order, 1)]


# Words that name the model under test or the builds it is compared with. A reviewer who sees
# them can guess the candidate, which breaks the blinding. Each distinct word becomes a stable
# neutral label (model-A, model-B, ...), so two builds in one case stay two different things.
MASK_TERMS = (
    "qwen3-coder-30b-a3b-instruct", "qwen3-coder", "qwen3", "qwen", "dwq", "4-bit", "4bit",
    "gguf", "q4_k_m", "mlx_lm", "mlx", "llama.cpp", "llama-server", "deepseek", "gpt",
    "gemini", "claude")


def mask_text(text: str, terms: Sequence[str] = MASK_TERMS) -> str:
    """Replace each term, case-insensitively, with the neutral label of that term."""
    ordered = sorted(set(t.lower() for t in terms), key=lambda t: (-len(t), t))
    labels = {t: f"model-{chr(65 + i % 26)}{i // 26 or ''}"
              for i, t in enumerate(sorted(set(ordered)))}
    pattern = re.compile("|".join(re.escape(t) for t in ordered), re.IGNORECASE)
    return pattern.sub(lambda m: labels[m.group(0).lower()], text)


def render_sheet(blinded: list[tuple[str, dict[str, Any]]],
                 mask: Sequence[str] = MASK_TERMS) -> str:
    sheet = _render_sheet(blinded)
    return mask_text(sheet, mask) if mask else sheet


def _render_sheet(blinded: list[tuple[str, dict[str, Any]]]) -> str:
    lines = [
        "# Review sheet: what should each research task become?",
        "",
        "For each case, decide the route it deserves, in your own judgement, from the "
        "claims and evidence shown. Do not look for the drafted labels: they are in the "
        "repository, so please finish this sheet first.",
        "",
        "## The routes",
        "",
    ]
    lines += [f"- **{route}**: {MEANINGS[route]}" for route in ROUTES]
    lines += [
        "",
        "Evidence either *supports* or *contradicts* its claim. A claim marked "
        "*checked by a person* has been confirmed after review.",
        "",
        "Copy `answers-template.json` to `answers.json` and replace each null with one route "
        "name.",
        "",
    ]
    for neutral, case in blinded:
        lines += [f"## {neutral}", ""]
        for number, claim in enumerate(case["claims"], 1):
            checked = ", checked by a person" if claim.get("verified") else ""
            lines.append(f"**Claim {number}** ({claim.get('kind', 'finding')}{checked}): "
                         f"{claim['text']}")
            evidence = claim.get("evidence", [])
            if not evidence:
                lines.append("- no evidence")
            for item in evidence:
                lines.append(f"- {item.get('relation', 'supports')}: source "
                             f"`{item['source']}` ({item['type']}): {item['text']}")
            lines.append("")
        lines += ["Route: ______", ""]
    return "\n".join(lines)


def answers_template(blinded: list[tuple[str, dict[str, Any]]]) -> dict[str, None]:
    return {neutral: None for neutral, _ in blinded}


def compare(cases: list[dict[str, Any]], answers: dict[str, Any], seed: int = DEFAULT_SEED,
            exclude: Sequence[str] = ()) -> dict[str, Any]:
    if not isinstance(answers, dict):
        raise ReviewError("the answers must be a JSON object of case id to route")
    blinded = blind(cases, seed, exclude)
    expected_ids = {neutral for neutral, _ in blinded}
    extra = set(answers) - expected_ids
    missing = expected_ids - {k for k, v in answers.items() if v is not None}
    if extra:
        raise ReviewError(f"answers for unknown cases: {sorted(extra)}")
    if missing:
        raise ReviewError(f"no answer for: {sorted(missing)}")
    bad = {k: v for k, v in answers.items() if v not in ROUTES}
    if bad:
        raise ReviewError(f"not a route name: {bad}")
    differences = [{"case": case["id"], "neutral": neutral, "drafted": case["expected"],
                    "reviewer": answers[neutral]}
                   for neutral, case in blinded if answers[neutral] != case["expected"]]
    confusion = Counter((case["expected"], answers[neutral]) for neutral, case in blinded)
    return {"total": len(blinded), "agree": len(blinded) - len(differences),
            "differences": differences,
            "confusion": {f"{d} -> {r}": n for (d, r), n in sorted(confusion.items())}}


def format_comparison(result: dict[str, Any]) -> str:
    lines = [f"agree on {result['agree']} of {result['total']} cases"]
    for diff in result["differences"]:
        lines.append(f"  differs: {diff['case']} ({diff['neutral']}): drafted "
                     f"{diff['drafted']}, reviewer {diff['reviewer']}")
    lines.append("drafted -> reviewer: " + ", ".join(
        f"{k} x{v}" for k, v in result["confusion"].items()))
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="lab.reviewsheet", description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("sheet", "compare"):
        p = sub.add_parser(name)
        p.add_argument("--cases", type=Path, required=True)
        p.add_argument("--seed", type=int, default=DEFAULT_SEED)
        p.add_argument("--exclude", nargs="*", default=[], metavar="ID",
                       help="case ids to leave out (for example cases that cannot be built)")
        if name == "sheet":
            p.add_argument("--out", type=Path, required=True, help="directory to write into")
            p.add_argument("--mask", nargs="*", default=None, metavar="TERM",
                           help="words to hide from the reviewer (default: the built-in list "
                                "of model and build names; pass --mask with no words for none)")
        else:
            p.add_argument("--answers", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        cases = load_cases(args.cases)
        if args.command == "sheet":
            blinded = blind(cases, args.seed, args.exclude)
            args.out.mkdir(parents=True, exist_ok=True)
            terms = MASK_TERMS if args.mask is None else tuple(args.mask)
            sheet = render_sheet(blinded, terms) + "\n"
            (args.out / "review-sheet.md").write_text(sheet, encoding="utf-8")
            template = json.dumps(answers_template(blinded), indent=2) + "\n"
            (args.out / "answers-template.json").write_text(template, encoding="utf-8")
            print(f"wrote {len(blinded)} cases to {args.out}")
            return 0
        print(format_comparison(compare(cases, json.loads(args.answers.read_text(encoding="utf-8")),
                                        args.seed, args.exclude)))
        return 0
    except (ReviewError, OSError, ValueError) as exc:
        print(f"reviewsheet: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
