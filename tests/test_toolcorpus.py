"""Held-out tool-call corpus for the pre-registered H2 and H2b (#76, #82)."""

from __future__ import annotations

import collections
import json

from lab import toolcorpus
from lab.broker import validate_params
from lab.evals import ROOT, grade, load_tasks


def test_the_corpus_is_deterministic_and_balanced() -> None:
    first, second = toolcorpus.build(), toolcorpus.build()
    assert first == second
    assert len(first) == 10 * len(toolcorpus.CORPUS_TOOLS)
    per_tool = collections.Counter(t["check"]["tool"] for t in first)
    assert set(per_tool) == set(toolcorpus.CORPUS_TOOLS) and set(per_tool.values()) == {10}
    assert len({t["id"] for t in first}) == len(first)
    assert len({t["prompt"] for t in first}) == len(first)


def test_every_expected_call_is_one_the_broker_accepts_and_the_grader_passes() -> None:
    for task in toolcorpus.build():
        check = task["check"]
        validate_params(check["tool"], check["arguments"])
        answer = json.dumps({"tool": check["tool"], "arguments": check["arguments"]})
        assert grade(check, answer), task["id"]


def test_no_path_overlaps_the_exploratory_prompts_already_seen() -> None:
    text = "\n".join(t["prompt"] for t in toolcorpus.build())
    for seen in toolcorpus.EXPLORATORY_PATHS:
        assert f" {seen}." not in text and f" {seen} " not in text, seen


def test_the_schema_variant_differs_only_by_its_prefix() -> None:
    plain, schema = toolcorpus.build(), toolcorpus.build(schema_in_prompt=True)
    for a, b in zip(plain, schema, strict=True):
        assert a["id"] == b["id"] and a["check"] == b["check"]
        assert b["prompt"] == toolcorpus.schema_prefix() + a["prompt"]


def test_the_committed_files_are_what_the_generator_writes() -> None:
    for schema_in_prompt, name in ((False, toolcorpus.PLAIN), (True, toolcorpus.SCHEMA)):
        path = ROOT / "evals" / name
        tasks, _ = load_tasks(path)
        expected = toolcorpus.build(schema_in_prompt=schema_in_prompt)
        assert [t.id for t in tasks] == [t["id"] for t in expected]
        assert path.read_text(encoding="utf-8") == toolcorpus.render(expected)
