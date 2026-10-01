"""A held-out tool-call corpus for the pre-registered H2 and H2b (#76, #82).

The three tool-call tasks in ``evals/tasks.jsonl`` move the refused-call rate
in steps of 0.33, and 48 more prompts were already run against the real model
before registration (setup section 20, exploratory). A confirmatory test needs
prompts nobody has seen a model answer, so this module generates them from
fixed word lists and a fixed seed, and the files it writes are frozen by
SHA-256 in the pre-registration amendment.

Design choices made before any of these prompts was run:

* Every broker tool gets the same number of tasks, so the corpus does not
  lean on the one tool (``fs.write``) that failed in the exploratory run.
* Paths, URLs and commands are disjoint from the exploratory prompts
  (``EXPLORATORY_PATHS``); a test enforces it.
* The prompt template is the one ``evals/tasks.jsonl`` already uses.
* The check names the exact arguments as well as the tool, so a call that
  parses but does the wrong thing does not pass.
* The schema variant is the same corpus with one prefix: the JSON Schema from
  ``lab.grammar``, as text. It tests H2b; everything else is identical.

Write the files with ``python -m lab.toolcorpus``.
"""

from __future__ import annotations

import json
import random
from typing import Any

from lab import grammar
from lab.broker import TOOL_SCHEMAS

SEED = 20260930
# The broker tools that existed when the corpus was registered. Fixed here,
# not read from the broker, so a tool added later (#240) does not change
# the frozen prompts or the schema prefix.
CORPUS_TOOLS = ("fs.read", "fs.write", "fs.delete", "fs.list", "shell.run", "net.fetch",
                "connector.call")
# Every broker tool that is not in the corpus, and why. A test fails when a
# broker tool is in neither list, so a new tool cannot reach a measured
# artifact, or stay out of one, without someone saying so here.
NOT_IN_CORPUS = {
    "fs.search": "added after registration (#240)",
    "git.status": "added after registration (#240)",
    "git.log": "added after registration (#240)",
    "git.diff": "added after registration (#240)",
    "net.summarize": "added after registration (#240)",
    # One generic tool for every signed MCP server (#256). The tools behind it
    # are the operator's choice and change with the config, so a call to one
    # is not a fixed prompt a held-out corpus can grade.
    "mcp.call": "added after registration, and its tools come from the signed config (#256)",
}
PER_TOOL = 10
PLAIN = "toolcalls-v1.jsonl"
SCHEMA = "toolcalls-v1-schema.jsonl"
FORMAT = ' Format: {"tool": ..., "arguments": {...}}'

# Paths used by the 48 exploratory prompts of 2026-09-30 (setup section 20).
EXPLORATORY_PATHS = ("a.txt", "notes.md", "src/main.py", "data/log.csv", "README.md",
                     "docs/plan.md", "tmp/x.json", "report.txt", "config.toml", "b c.txt",
                     "ünï.md", "deep/a/b/c.txt", "out.log", "todo.md", "lab.db", "script.sh")

DIRS = ("inventory", "ledger/2026", "fixtures/io", "assets/img", "archive/q2",
        "meetings/weekly", "build/out", "vendor/manuals", "drafts/essays", "photos/raw")
NAMES = ("summary", "budget", "roster", "changelog", "invoice-17", "outline", "metrics",
         "packing-list", "glossary", "timeline")
EXTS = (".md", ".csv", ".txt", ".json", ".yaml")
CONTENTS = ("done", "ready for review", "v2 approved", "total 42", "see ticket 881",
            "moved to Friday", "no changes", "all tests green", "call back at 3", "draft 3")
URLS = ("https://example.org/status", "https://example.com/feeds/latest.xml",
        "https://example.net/api/v2/health", "https://example.org/docs/intro.html",
        "https://example.com/releases/2026-09.json", "https://example.net/robots.txt",
        "https://example.org/wiki/Main_Page", "https://example.com/images/logo.svg",
        "https://example.net/search?q=sqlite", "https://example.org/changelog.md")
COMMANDS = (["git", "status"], ["python3", "--version"], ["uname", "-a"], ["df", "-h"],
            ["wc", "-l", "inventory.csv"], ["echo", "hello"], ["whoami"], ["pwd"],
            ["ls", "-la", "build"], ["sw_vers", "-productVersion"])
CONNECTORS = ("github", "mastodon", "calendar", "weather", "notes")
METHODS = ("GET", "POST", "DELETE")
HERE = ("lists the current directory", "lists the files in the working directory",
        "shows what is in the current folder", "lists the directory it is running in",
        "lists the working directory without naming a path")


def _signature(tool: str) -> str:
    params = (n if required else f"{n} optional" for n, (_, required) in TOOL_SCHEMAS[tool].items())
    return f"{tool}({', '.join(params)})"


def _paths(rng: random.Random, n: int) -> list[str]:
    combos = [f"{d}/{name}{ext}" for d in DIRS for name in NAMES for ext in EXTS]
    return rng.sample(combos, n)


def _task(tool: str, action: str, arguments: dict[str, Any], number: int) -> dict[str, Any]:
    prompt = (f"Respond with only a JSON tool call that {action}. "
              f"Tools: {_signature(tool)}.{FORMAT}")
    return {"id": f"tc-{tool.replace('.', '-')}-{number:02d}", "prompt": prompt,
            "check": {"type": "tool_call", "tool": tool, "arguments": arguments}}


def build(*, schema_in_prompt: bool = False, seed: int = SEED) -> list[dict[str, Any]]:
    rng = random.Random(seed)
    paths = iter(_paths(rng, 4 * PER_TOOL))
    tasks: list[dict[str, Any]] = []
    for i in range(PER_TOOL):
        path = next(paths)
        tasks.append(_task("fs.read", f"reads the file {path}", {"path": path}, i))
    for i in range(PER_TOOL):
        path, content = next(paths), CONTENTS[i]
        tasks.append(_task("fs.write", f'writes the text "{content}" to the file {path}',
                           {"path": path, "content": content}, i))
    for i in range(PER_TOOL):
        path = next(paths)
        tasks.append(_task("fs.delete", f"deletes the file {path}", {"path": path}, i))
    for i in range(PER_TOOL):
        if i % 2:
            tasks.append(_task("fs.list", HERE[i // 2], {}, i))
        else:
            folder = next(paths).rsplit("/", 1)[0]
            tasks.append(_task("fs.list", f"lists the directory {folder}",
                               {"path": folder}, i))
    for i, argv in enumerate(COMMANDS):
        words = " ".join(argv)
        if i % 3 == 0:
            tasks.append(_task("shell.run", f"runs the command `{words}` as an argument list "
                               "with a timeout of 30 seconds", {"argv": argv, "timeout": 30}, i))
        else:
            tasks.append(_task("shell.run", f"runs the command `{words}` as an argument list",
                               {"argv": argv}, i))
    for i, url in enumerate(URLS):
        tasks.append(_task("net.fetch", f"fetches the URL {url}", {"url": url}, i))
    for i in range(PER_TOOL):
        connector, item = CONNECTORS[i % len(CONNECTORS)], rng.randint(100, 999)
        arguments: dict[str, Any] = {"connector": connector, "path": f"/v1/items/{item}"}
        action = f"calls the {connector} connector at path /v1/items/{item}"
        if i % 2 == 0:
            arguments["method"] = METHODS[i % len(METHODS)]
            action += f" with method {arguments['method']}"
        tasks.append(_task("connector.call", action, arguments, i))
    if schema_in_prompt:
        for task in tasks:
            task["prompt"] = schema_prefix() + task["prompt"]
    return tasks


def schema_prefix() -> str:
    schema = grammar.response_format(set(CORPUS_TOOLS))["json_schema"]["schema"]
    return ("Reply with one JSON object that validates against this JSON Schema, "
            f"with no other keys: {json.dumps(schema, sort_keys=True)}\n\n")


def render(tasks: list[dict[str, Any]]) -> str:
    return "".join(json.dumps(t, sort_keys=True, ensure_ascii=False) + "\n" for t in tasks)


def main() -> int:
    from lab.evals import ROOT, load_tasks
    for schema_in_prompt, name in ((False, PLAIN), (True, SCHEMA)):
        path = ROOT / "evals" / name
        path.write_text(render(build(schema_in_prompt=schema_in_prompt)), encoding="utf-8")
        _, sha = load_tasks(path)
        print(f"{name}  sha256 {sha}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
