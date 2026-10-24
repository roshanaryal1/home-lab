# Dependency supply-chain checks

Tracked in #430 and the January milestone #341.

## What CI checks

`.github/workflows/supply-chain.yml` runs on pull requests to `main`
and on pushes to `main`. It uses the committed `uv.lock`, and it does not
write an updated lockfile.

1. `uv audit --locked` checks the resolved dependency set, including optional
   and development dependencies, for known vulnerabilities and adverse package
   statuses. It queries OSV over the network and fails the job for a failed
   audit or service error. It does not claim to detect undisclosed defects.
2. `uv export --locked --format cyclonedx1.5 --output-file home-lab.cdx.json`
   exports a CycloneDX v1.5 SBOM from the same lockfile. The job checks its
   format and verifies that the document contains named components.
3. The workflow stores `home-lab.cdx.json` as an Actions artifact named
   `python-dependencies-cyclonedx`, retained for 14 days. Reviewers can
   inspect it before merging.

The checkout action does not persist Git credentials and the workflow has
read-only repository permissions. All actions are pinned by full commit SHA.

## Run locally

Use the uv version provided by your development environment:

```sh
uv --version
uv audit --locked
uv export --locked --format cyclonedx1.5 --output-file home-lab.cdx.json
```

The audit requires network access. This is separate from the project's
nightly runtime safety tests and does not replace them. The CycloneDX export
format is in preview as of 2026-10-24, so verify it after an uv upgrade.

Only the Python dependencies represented by `uv.lock` are covered. This
is not a full inventory of the Mac, downloaded model weights, OS packages,
Apple containers or remote services. It is not a signed release attestation
and does not prove provenance of a published binary.

## References

- [uv audit command](https://docs.astral.sh/uv/reference/cli/#uv-audit)
- [uv CycloneDX export](https://docs.astral.sh/uv/concepts/projects/export/#cyclonedx-sbom-format)
- [Supply-chain milestone](https://github.com/roshanaryal1/home-lab/issues/341)
