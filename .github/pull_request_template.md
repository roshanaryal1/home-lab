## Closes

<!-- Closes #N. Every PR needs an issue. If there isn't one, open it first. -->
Closes #

## What changed

## Evidence

<!--
Not "should work". What was run, and what it printed.
If this fixes a bug, show the before and after measurement.
-->

```
```

## Checks

- [ ] `python3 -m pytest tests/ -q` passes
- [ ] `ruff check .` clean
- [ ] a test fails before this change and passes after
- [ ] acceptance criteria on the linked issue are all ticked
- [ ] docs updated in this PR, not "later"
- [ ] touches `lab/queue.py` recover(), the supervisor slots, or the approval
      path? If yes, say so here and explain why the safety properties still hold
