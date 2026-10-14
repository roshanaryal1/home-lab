# Browser in the task container: design (#369)

Written 2026-10-09. This is a design only. No code, tool or image exists yet. It is feature 16 in [FEATURE-PLAN.md](FEATURE-PLAN.md). It builds on the egress gateway in [lab/egress.py](../lab/egress.py), the broker's tier table in [lab/broker.py](../lab/broker.py) and the container tier in [ADR 0007](decisions/0007-isolation-for-untrusted-code.md).

Facts marked (unverified) were not checked against a primary source. Sources are numbered in section 10.

## 1. What this is

`browser.fetch` opens one web page in a headless Chromium browser inside the task's disposable container. It waits for the page to render, then returns the visible text as tainted input, with the final URL and the HTTP status. Version 1 reads pages only. It does not click, fill forms or log in. Some pages show their text only after JavaScript runs, and `net.fetch` returns the raw response body, so those pages need a browser. A browser is a much larger program than an HTTP client. It therefore runs only in the container tier, and it reaches the web only through the egress gateway.

## 2. What it must never do

- Run on the host. It runs only in a container. With no container runtime on the host, the tool refuses, as `skill.run` does.
- Use the owner's browser, its profile, its cookies or its sign-ins.
- Reach any host except through the egress gateway, under the task's allowlist.
- Keep cookies, storage or cache from one task to the next.
- Download a file, or write anything to the task workspace except the result it returns.

## 3. Where it runs and how

- **Image.** A browser image of its own, separate from the `skill.run` image. It is pinned by digest. `check_image` in [lab/container.py](../lab/container.py) already refuses any other form. The digest does not exist yet. It is recorded when the image is built.
- **Browser and library.** Chromium, driven by Playwright for Python, version 1.63.0. PyPI lists it as the latest release, dated 2026-09-15 (source 5). It needs Python 3.10 or later (source 5).
- **Base image.** Playwright publishes images named `mcr.microsoft.com/playwright/python:v<version>-<ubuntu codename>`, such as `-noble` (source 6). The tag `v1.63.0-noble` is not checked against the registry (unverified).
- **Image size.** Unverified. Playwright's Docker page does not state it (source 6). The lab measures the built image before it pins the digest.
- **Guest size.** The container tier defaults to 2 CPUs and 1024 MB, with ceilings of 4 CPUs and 2048 MB (`lab/container.py`). The design starts the browser at the 2048 MB ceiling. ADR 0007 measured about 2 GiB of host memory for a busy 1024 MB guest. Browser memory is not measured yet (see section 6).
- **Entry point.** A reviewed script inside the image, for example `/opt/lab/browse`. The URL is its one argument. It prints the rendered text and the status. The task never supplies the script.
- **Profile.** A new profile directory for each run, under `/tmp`. In the guest, `/tmp` is a tmpfs, so the profile lives in guest memory and ends with the VM. The browser never writes to `/work`. The container builder sets `HOME=/work` for `skill.run`, so the browser builder sets its own `HOME` under `/tmp`. Otherwise the browser cache would land in the workspace and outlive the task.
- **Engine options.** Headless. The profile path is explicit, passed to `launch_persistent_context`. Playwright's documentation says an empty path creates a temporary directory (source 8). The design passes its own path, so the lab knows where the profile is. Downloads are refused with the context option `accept_downloads=False`. The Node name is `acceptDownloads`, which defaults to true (source 7). The Python spelling is unverified.
- **Proxy.** A `proxy` launch option points the browser at the gateway (source 7). Playwright turns it into `--proxy-server=` (source 9). The design also passes `--host-resolver-rules` with `MAP * ~NOTFOUND`, excluding the gateway address. Playwright uses this rule for SOCKS proxies (source 9). Using it for the HTTP proxy is a design choice, so the browser never resolves a name itself.

## 4. Network

### Today's path for `net.fetch`

The broker opens each task with its own allowlist (`open_workspace`, `egress_hosts`). The task cannot change the list. Before a request goes out, the broker writes an `egress_intent` audit row. If that write fails, the request is not made. The gateway in `lab/egress.py` then:

- allows only https on port 443, to DNS names on the list. It refuses IP literals and credentials in the URL.
- resolves the name once. It refuses the whole answer if any address is not globally routable.
- connects to the address it checked. It never does a second lookup.
- follows a redirect only by checking the new hop again, at most 3 hops.
- caps the response at 2 MiB and the wait at 15 seconds.
- returns the body as fixed-schema evidence, never as raw text.

Each attempt is audited with the host and a hash of the URL. The query string is never logged.

### What the browser adds

A page makes many requests: the document, scripts, styles, images, fonts and calls that scripts make. The gateway serves one request per call, so it cannot carry a browser's connections. This design adds a **tunnel mode** to `lab/egress.py`. It is new code. The browser sends `CONNECT host:443` to the gateway, and the gateway then:

1. refuses any port other than 443. It refuses any target that is not a DNS name.
2. checks the host against the task's allowlist. It resolves the host with the checks in `validate()`, by calling `validate()` on `https://<host>/`.
3. connects to the checked address, and answers `200 Connection established`.
4. copies bytes in both directions, under a byte cap and a time cap.

TLS stays end to end. The browser checks the certificate against the host name, as it does for any site. The checked address is only where the connection goes.

Each CONNECT is audited as `egress_allow` or `egress_deny`, with the host and a hash of the target. The path and query are never logged. A redirect is a new CONNECT, so each redirect passes the same checks. That covers the metadata-address and off-list cases that `tests/test_egress.py` already tests for `net.fetch`.

### The guest gets no other route

The guest must have exactly one route out, to the gateway. Today the container executor passes `--network none`. ADR 0007 measured that this leaves only loopback. Apple's `container` documentation does not yet show how to keep that and add one route:

- The command reference documents `--network <name>` for attaching a container to a network. It does not document `none` (source 2). The lab measured `--network none` on 1.5.0 (ADR 0007).
- It documents `--publish-socket host_path:container_path` as "Publish a socket from container to host" (source 2). The text describes a socket that runs from the container to the host. The format lists the host path first. So the reference does not show whether a host socket can be passed into the guest (unverified).
- Third-party summaries say macOS 26 can create isolated networks with `container network create` and a chosen subnet (source 4, unverified against Apple's own docs).

Two options remain open. The owner chooses after a test on the M6 (question 1).

- **Option A: a host socket into a guest with no network.** The gateway listens on a Unix socket on the host. The guest reaches it through a mount or a published socket, and keeps `--network none`. The guest would then have no IP route at all. Unverified.
- **Option B: a dedicated network and a host firewall rule.** The browser gets its own network. The gateway listens on that network's host address. A host firewall rule (the macOS packet filter, pf) drops every other destination from that network. Unverified. The rule becomes part of the boundary, so the owner's machine must hold it, and a nightly self-test must check it.

The default network is not an option. ADR 0007 measured that it reaches the internet.

The browser's run command gets its own builder. That builder accepts no network value from a caller. It does not reuse `build_run_argv`, which fixes `--network none` for `skill.run`. The docstring of `lab/container.py` says `--network none` is always passed. The browser builder then becomes the one documented exception, and the docstring must say so.

### What the tunnel cannot see

After a CONNECT succeeds, the gateway sees only the host and the port. The method, path, query and body are inside TLS. So version 1 cannot stop a page's script from sending data to an allowed host. The controls are these: no secrets in the guest, no sign-ins, no cookies between tasks, the owner's allowlist, and the Rule of Two in section 5. Question 4 asks whether to add TLS interception.

## 5. The tool and its tier

- **Schema.** One required parameter, `url`, a string. It has the same shape as `net.fetch` in `TOOL_SCHEMAS`. Any other parameter is refused.
- **Tier.** `Tier.NOTIFY`, like `net.fetch` and `net.summarize`. It goes in `TOOL_TIERS`.
- **Legs.** `UNTRUSTED_INPUT` and `EXTERNAL_ACTION`, the same as `net.fetch`, in `TOOL_LEGS`. A tool with no entry is treated as external, which fails closed, so the entry is required (`lab/authority.py`). Under the Rule of Two, a task that holds these two legs cannot also hold sensitive data.
- **Effect.** `IDEMPOTENT` in `TOOL_EFFECTS`. A retry loads the page again. The result's hash records what was read.
- **Handler.** A worker-process handler, `lab/handlers/browse.py`, that holds only `browser.fetch`. `lab/handlers/web.py` works the same way with `net.summarize`. The handler never sees raw HTML.
- **Result shape.**

```json
{
  "url": "https://final.example/path",
  "status": 200,
  "evidence": {"source_type": "web", "source_id": "https://example/", "sha256": "<64 hex>", "length": 0, "excerpt": "", "truncated": false},
  "screenshot_sha256": null,
  "untrusted": true
}
```

The host builds the evidence record with `extract_evidence` from the guest's text. The guest therefore never sets the schema. The excerpt holds visible text, with control and format characters removed by `clean()` (`lab/untrusted.py`). It is capped at 8000 characters. The host reads at most 256 KiB from the guest and drops the rest.

- **Screenshot.** A screenshot may be taken for its hash only. Its bytes are never returned, never stored in the workspace and never shown to the model. The hash lets the audit record tell two renders apart. Hidden text in a screenshot has steered an AI browser before (source 13, unverified).
- **Taint.** The task is tainted with the reason "read a page rendered in the browser". The taint is set on any run that reached the browser, including a run that then failed. `net.fetch` taints only on success. A browser can read a page and then fail, so the browser taints earlier.
- **Refusals before anything starts.** A URL that is not https, has credentials, is an IP literal or names a host off the list is refused by the same checks as `validate()`. With no container runtime, the tool refuses.

## 6. Limits

These values are proposed. None is measured yet.

| Limit | Proposed value | Enforced by | Basis |
|---|---|---|---|
| Wall time per page | 20 s to render, 30 s for the whole run | The reviewed script's page timer, and the executor's timeout | The ceiling `MAX_TIMEOUT_SECONDS` is 300 s (`lab/sandbox.py`) |
| Guest memory | 2048 MB | `--memory` in the browser run command | The ceiling `MAX_MEMORY_MB` is 2048 (`lab/container.py`). Browser cost not measured |
| Guest CPUs | 2 | `--cpus` | The default `DEFAULT_CPUS` is 2 |
| Bytes per response | 2 MiB | The gateway tunnel, outside the guest | The same cap as `MAX_RESPONSE_BYTES` for `net.fetch` |
| Bytes per page | 8 MiB across all tunnels | The gateway, outside the guest | Proposed |
| Tunnels per page | 60 | The gateway, outside the guest | Proposed |
| Pages per task | 5 `browser.fetch` calls | The broker, per task | Proposed. `MAX_CALLS_PER_TASK` (1000) stays the outer limit |
| Output from the guest | 256 KiB | `ContainerExecutor`, which drops the rest | `MAX_OUTPUT_BYTES` |
| Text returned to the model | 8000 characters | `extract_evidence` on the host | `MAX_LIMIT` in `lab/untrusted.py` |

Every cap that protects the lab sits on the host side of the boundary. The guest is untrusted, so a guest cannot lift its own cap.

## 7. Tests before it ships

1. **Injection suite (feature 17, #368).** Add browser cases to the public suite. Each case is a hostile page with hidden instructions, fetched with `browser.fetch`. The suite grades state: the workspace files, the database and what the simulated network saw. It does not grade the model's words. Attack success must stay at zero. The grading follows the pattern in `lab/attacks.py` (item 4.7).
2. **Network boundary on the real container.** On the M6, from inside the browser container, try each of these: a host off the allowlist by name, an IP literal, the metadata address, a port other than 443 on an allowed host, UDP DNS, and a direct route to a public address that skips the gateway. Every attempt must fail. Register the claim before the run, as claim M5 was registered (`docs/PREREGISTRATION-SAFETY.md`).
3. **Network boundary in CI.** With a fake container runtime and the fake transport from `tests/test_egress.py`, check the browser run's argument list. It carries no network option except the designated one. Each refusal in test 2 that the tunnel can make also runs here.
4. **No profile survives a task.** Task A sets a cookie and writes local storage, using a test page on an allowed host. Task B then looks for that cookie and storage, and must find none. After each run, the workspace holds no browser file, and the container is gone. `tests/test_container.py` already tests removal on every path.
5. **No download.** A test page that starts a download writes nothing to the workspace.
6. **Redirects.** A redirect to the metadata address, and a redirect to an off-list host, each stop at the tunnel. `tests/test_egress.py` covers the same cases for `net.fetch`.
7. **Logs.** The audit never contains a path or a query string.
8. **Limits.** A page that never finishes loading stops at the time cap. A page that opens more tunnels than the cap stops at the tunnel cap.

## 8. Threat model

| Threat | What stops it |
|---|---|
| A page makes the browser reach a host off the allowlist | The guest has no other route. The gateway checks the host name, the port and every resolved address. |
| A page names an IP address, or a name that resolves to a private address | An IP literal never matches a DNS-name allowlist entry. A single non-global address refuses the whole answer. |
| A redirect sends the browser to the metadata address or to an off-list host | Each redirect is a new tunnel, and each one passes the same checks. |
| DNS rebinding between the check and the connection | The gateway connects to the address it checked. It does not look the name up again. |
| A page reads a sign-in or a cookie from an earlier task | Each run gets a new profile in guest memory, which ends with the VM. The guest gets only allowlisted environment variables. |
| A browser cache or profile stays in the workspace | The profile and cache live under `/tmp`, never under `/work`. A test checks the workspace after each run. |
| A page downloads a file into the workspace | Downloads are refused in the browser context. A test checks it. |
| Hidden text in a page steers the model | The model receives fixed-schema evidence as data. The task is tainted, and the Rule of Two keeps the task away from sensitive data. |
| Hidden text in a screenshot steers the model | The screenshot is hashed. Its bytes are never returned or shown to the model. |
| A flaw in the browser escapes to the host | The browser runs in its own VM (ADR 0007). The guest runs as uid 65534, and its root file system is read-only. |
| A page exhausts memory, time or bandwidth | The container limits, the time cap and the byte caps on the host side. The container is removed on every path out. |
| A page sends data to an allowed host inside TLS | No network control stops this in version 1. The limits are no secrets in the guest, no sign-ins, the owner's allowlist and the Rule of Two. Question 4 covers this. |

## 9. Open questions for the owner

1. Apple's `container` tool: Option A (a host socket into a guest with no network) or Option B (a dedicated network plus a host firewall rule)? If Option B, who installs the firewall rule, and which nightly self-test checks it?
2. Chromium's own sandbox inside the guest: keep it on, which may need a kernel feature the guest does not offer (unverified), or turn it off, because the VM is the boundary? Test this first.
3. JavaScript on or off? On renders more pages, and runs more code from the page. Off is safer, but some pages then show no text.
4. Method and path control: accept host-only control in version 1, or add TLS interception with a per-task certificate authority? Interception puts a root certificate in the guest, and a signing key that must be protected.
5. Screenshot hash only, as proposed, or no screenshot at all in version 1?
6. The limits in section 6. The owner approves them after the M6 measures a real page load.
7. Who builds and pins the browser image, and how often it moves with Playwright's releases? The current latest release is 1.63.0 (source 5).
8. Pages per task: 5, as proposed. The owner decides.

## 10. Sources

1. Apple, `container` releases, https://github.com/apple/container/releases, read 2026-10-09. The latest release is 1.5.0, dated "29 Sep". The page prints no year. ADR 0007 records the install on 2026-09-29. The previous release, 1.0.0, is dated "09 Jun".
2. Apple, `container` command reference (main branch), https://raw.githubusercontent.com/apple/container/main/docs/command-reference.md, read 2026-10-09. It covers `--network`, `--publish-socket`, `--read-only` and `--tmpfs`. It does not name a version.
3. Apple, `container` how-to (main branch), https://raw.githubusercontent.com/apple/container/main/docs/how-to.md, read 2026-10-09. It has no text on `--network none` or custom networks.
4. Third-party summaries of `container network create` on macOS 26 (instagit.com pages), found by search on 2026-10-09 and not read in full. Unverified against Apple's documentation.
5. Playwright for Python, PyPI, https://pypi.org/project/playwright/, read 2026-10-09. The latest version is 1.63.0, released 2026-09-15. It requires Python 3.10 or later.
6. Playwright, Docker, https://raw.githubusercontent.com/microsoft/playwright/main/docs/src/docker.md, read 2026-10-09. It gives the image name pattern and tag format. It states no image size.
7. Playwright, API parameters, https://raw.githubusercontent.com/microsoft/playwright/main/docs/src/api/params.md, read 2026-10-09. It covers the `proxy` fields, `acceptDownloads` (default true) and `downloadsPath`.
8. Playwright, `browserType.launchPersistentContext`, https://raw.githubusercontent.com/microsoft/playwright/main/docs/src/api/class-browsertype.md, read 2026-10-09. An empty `userDataDir` creates a temporary directory.
9. Playwright source, `chromium.ts`, https://raw.githubusercontent.com/microsoft/playwright/main/packages/playwright-core/src/server/chromium/chromium.ts, read 2026-10-09. It adds `--proxy-server=` for the proxy, and `--host-resolver-rules` for SOCKS.
10. Microsoft, Edge policy `DownloadRestrictions`, https://learn.microsoft.com/en-us/deployedge/microsoft-edge-browser-policies/downloadrestrictions, found by search on 2026-10-09 and not read. Search results summarized it as a value 3 that blocks all downloads (unverified). This is an Edge policy, not confirmed for Chromium. The design does not use it.
11. Anthropic, "Piloting Claude in Chrome", 2025-08-25, https://claude.com/blog/claude-for-chrome (the earlier address redirects to it), read 2026-10-09. The post says browser-using AIs face prompt injection, where attackers hide instructions in websites, emails or documents.
12. Anthropic, "Mitigating the risk of prompt injections in browser use", 2025-11-24, https://www.anthropic.com/research/prompt-injection-defenses, read 2026-10-09. The post says browser use widens the risk. Any page, document, advertisement or script can carry instructions, and each action the agent can take becomes a target.
13. Brave, "Unseeable prompt injections in screenshots: more vulnerabilities in Comet and other AI browsers", October 2025, https://brave.com/blog/unseeable-prompt-injections/. Not read directly, because the site did not resolve from the research environment. The date and content come from search results on 2026-10-09 and from a 2025-10-21 post by Simon Willison that links to it (unverified).
