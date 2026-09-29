# Releasing a citable version (item 8.2, #83)

`CITATION.cff` at the repo root is validated in CI (`tests/test_citation.py`)
and kept in step with `pyproject.toml`. A DOI needs one thing only the
repository owner can do.

## Once, by the owner

- [ ] Sign in to Zenodo with the GitHub account that owns the repository and
      switch the `roshanaryal1/home-lab` toggle on under GitHub integration.
      (The repository is public, which is what Zenodo needs.)
- [ ] Optionally add an ORCID iD under `authors` in `CITATION.cff`.

## Per paper, or per version worth citing

- [ ] Bump `version` in `pyproject.toml` and in `CITATION.cff` together, and
      set `date-released`. The test fails if they differ.
- [ ] Run the evaluation on the mini (`lab eval run`, section 14 of
      `ops/mac-mini-setup.md`) and commit the sealed record, so the release
      contains the exact code, task set and result.
- [ ] Publish a GitHub release for that tag. Zenodo archives it and mints a
      DOI; paste the DOI badge into the README and add `doi:` to `CITATION.cff`
      in the next commit.
