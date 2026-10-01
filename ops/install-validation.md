# Testing the install guide on a fresh Mac

The install guide, [docs/INSTALL.md](../docs/INSTALL.md), has not been run end
to end from a clean start by anyone but its author. That test is the open
acceptance item of [#188](https://github.com/roshanaryal1/home-lab/issues/188). Do
not close the issue because the text reads well or CI passes: run it on a real
Apple silicon Mac, from a fresh macOS user account if you can, and write down
what you see. If a command fails, fix the guide or the code first.

A second Mac, or a second user on the MacBook, is enough. It does not need the
Mac mini.

Run these in order:

1. **Prerequisites.**
   ```sh
   ./scripts/check-prerequisites.sh
   ```
   Confirm it changes nothing, reports Apple silicon and the right unified
   memory, and labels the model tier as measured (32 GB) or untested (other sizes).

2. **Repository and CLI.**
   ```sh
   uv sync --locked
   uv run python -m lab.cli --help
   uv run python -m lab.cli operator init --dir "$HOME/.lab-operator"
   ```

3. **Setup plan.**
   ```sh
   uv run python -m lab.cli setup-plan --operator-pubkey "$HOME/.lab-operator/operator.pub"
   ```
   Read it before applying. Stop if it contains a path that belongs to someone
   else's machine or a model you did not choose.

4. **Apply and isolation.** Run the `sudo ... setup-plan --apply` command from
   the guide, then confirm all five checks in guide section 5: `lab` cannot use
   `sudo`, is not an admin, cannot read the operator's private key, cannot write
   a service definition, and cannot list the operator's home folder. Confirm only
   `operator.pub` was copied into `/etc/homelab`.

5. **Deployment.** Follow the runbook (`ops/runbook-lab-account-and-daemons.md`)
   from its step 3, with `REPO`, `COMMIT`, `MODEL_REV` and `BACKUP_VOLUME` set. The
   model name is read from the server in step 4; check that the value it reads
   is a path and that a request using it is accepted (HTTP 200). Write down any
   command that differs from the guide before you change the guide.

6. **Services and health.** All six launchd jobs load, the supervisor runs as
   `lab`, and these work:
   ```sh
   sudo -u lab /opt/homelab/.venv/bin/python -m lab.cli --db /var/homelab/lab.db status
   sudo -u lab /opt/homelab/.venv/bin/python -m lab.cli --db /var/homelab/lab.db audit verify
   ```

7. **Model serving.** Start the server with the commands in guide section 8 and
   note how long the download took and whether anything differed. The served model
   is the pinned revision, the server listens only on `127.0.0.1:8080`, and one
   local request succeeds. Do not expose it to the network. The guide has no
   auto-start (LaunchAgent) for the server yet; record what you would need.

8. **Recovery.** Run the two timed drills in runbook step 6 and record the
   numbers. Also do the separate startup-recovery drill (a task in flight when the
   supervisor dies), which the runbook lists as still open.

9. **Teardown.** Run the guide's uninstall section on a machine with nothing you
   want to keep, and confirm services are unloaded and the deployment is gone
   without touching data you had not backed up.

Record the outputs (what ran, on which Mac and macOS version, what you saw) in
the issue, and commit a dated note here or under `ops/drills/log/`.
