# Shell script checks

Tracked in #432 under the supply-chain milestone #341.

The `shell-lint` GitHub Actions workflow checks every tracked `*.sh`
file on pull requests to `main` and pushes to `main`.

- Git enumerates the files, so generated or untracked shell scripts are not
  silently included in CI.
- The first line determines whether `bash -n` or `sh -n` performs the
  syntax check. An unrecognized interpreter fails instead of being guessed.
- ShellCheck v0.11.0 performs static analysis at warning severity or higher.
- A pinned SHA-256 is verified before the downloaded Linux x86_64 binary is
  extracted and run. Action dependencies are also pinned by full commit SHA.
- The script paths are printed. CI never executes a project's setup,
  restore, or maintenance script. Syntax checks parse but do not run them.

## Running locally

On Linux, with a trusted ShellCheck v0.11.0 installed and a Bash shell:

```sh
files=()
while IFS= read -r -d '' path; do
  case "$(head -n 1 -- "$path")" in
    *bash*) bash -n "$path" ;;
    *sh*) sh -n "$path" ;;
    *) echo "unrecognized interpreter: $path" >&2; exit 1 ;;
  esac
  files+=("$path")
done < <(git ls-files -z -- '*.sh')
shellcheck --severity=warning -- "${files[@]}"
```

This validates only tracked `*.sh` files and their static structure.
It cannot prove that a script's privileged operations are safe on the
Mac mini or that an interrupted restore works. The owner must still perform
the Mac-only validation steps in `docs/STATUS.md` and the runbooks.
