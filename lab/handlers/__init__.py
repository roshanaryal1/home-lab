"""Reviewed handler code, the only code a worker process will load.

A handler here is ordinary, code-reviewed Python in this repository. The
worker refuses to import anything outside this package, so a task, a
payload or a model can name which reviewed handler runs, never supply
the code (item 1.2, #48).
"""


def register_all(supervisor: object) -> None:
    """Register every reviewed handler on a supervisor started as a daemon.

    Deliberately empty until a real handler ships: the demo handlers exist
    for tests and must not be reachable by tasks on a running lab. Add a
    ``supervisor.register_reviewed(...)`` call here in the same change
    that adds the handler.
    """
