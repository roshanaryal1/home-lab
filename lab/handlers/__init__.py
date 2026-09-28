"""Reviewed handler code, the only code a worker process will load.

A handler here is ordinary, code-reviewed Python in this repository. The
worker refuses to import anything outside this package, so a task, a
payload or a model can name which reviewed handler runs, never supply
the code (item 1.2, #48).
"""
