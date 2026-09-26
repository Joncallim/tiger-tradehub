# TradeHub deployment model

## The model in one paragraph

`/opt/tiger-tradehub` is a **git clone that production executes directly**. It is
checked out **detached at an immutable commit SHA** — never parked on a branch.
The revision actually deployed is recorded in a machine-readable, host-local
manifest (`deployment.json`), written by committed source
(`deploy/deployment_cli.py`) and validated on every read. An operator proves what
is running with one command; drift, configuration and rollback are all explicit.

```bash
python /opt/tiger-tradehub/deploy/deployment_cli.py status   # what is running?
python /opt/tiger-tradehub/deploy/deployment_cli.py verify   # prove it (exit != 0 on any doubt)
```

## Authoritative revision

* The authoritative branch is **`main`** (the default branch).
* The deployed identity is an **immutable commit SHA**, plus the **tree hash** it
  produced. Trees — not branch names — are what make a squash-merged PR
  provably equivalent to the commit that was reviewed: `main`'s tree was
  byte-identical to the reviewed commit's tree.
* Branch names appear in the manifest only as *provenance about* the SHA
  (`source_branch`, `source_branch_tip`), never as the deployed identity.

Do **not** track a branch in the deployed checkout. A branch ref is mutable: a
`git checkout main` in the deployed clone would silently change what production
executes. Check out the SHA (detached) and record it.

## `deployment.json` (host-local, gitignored)

| Field | Meaning |
|---|---|
| `manifest_version` | Schema version; unknown versions are rejected |
| `deployed_commit` | **The deployed revision** — full 40-hex commit SHA |
| `deployed_tree` | Tree hash that commit produced; must match the commit |
| `deployed_at` | ISO-8601 UTC timestamp of the record |
| `source_branch` / `source_branch_tip` | Provenance only (e.g. `main` @ its tip) |
| `source_release` | Release/tag name if the revision came from one (none exist today) |
| `previous_commit` | **The rollback target** — the revision deployed before this one |
| `previous_deployed_at` | When that prior revision was deployed (null if unknown) |
| `deploy_mechanism`, `deploy_mechanism_version` | How the record was produced |
| `dirty_at_deploy`, `dirty_paths_at_deploy` | Working-tree state at record time |
| `equivalent_commits` | Other commits with a **provably identical tree** (squash equivalence) |
| `host_local_config` | Declared configuration paths outside the checkout (**paths only**, never contents) |
| `host`, `tree_state` | Hostname and a clean/dirty summary |

Every SHA the manifest names is checked to exist in this clone, and every
equivalence claim is verified against the deployed tree. A manifest that is
absent, malformed, of an unknown version, naming an absent commit, or whose tree
does not match, is **rejected** — never treated as verified.

### `DEPLOYED_COMMIT` is retired

The old untracked `DEPLOYED_COMMIT` marker is **gone**. Its name asserted "the
commit currently deployed", but its only writer (`deploy/fa06_acceptance.py`)
recorded `git rev-parse HEAD` at the *start of an acceptance run* — so it went
stale on any deploy that did not re-run FA-06, and nothing consumed it to
notice. Its value is preserved where it belongs: as `previous_commit` (the
rollback target) in the manifest. `record` removes a leftover marker and
`verify` reports one if it reappears.

## Deploying a revision

```bash
# 1. safety: snapshot state, confirm the broker guard, capture the previous record
cd /opt/tiger-tradehub
sudo -u jon git rev-parse HEAD
python deploy/deployment_cli.py verify --json
cat /var/lib/tradehub/autonomy/kill_switch          # expect CLEARED
grep -c '^TRADEHUB_DRY_RUN=true' /etc/tradehub/execution.env /etc/tradehub/autonomy.env

# 2. fetch and choose an IMMUTABLE revision
sudo -u jon git fetch origin
REV=$(sudo -u jon git rev-parse origin/main)        # or a reviewed SHA

# 3. prove the tree you are about to run is the tree that was reviewed
sudo -u jon git rev-parse "${REV}^{tree}"

# 4. check out the revision detached, restart, record, prove
sudo -u jon git checkout --quiet --detach "$REV"
systemctl restart tradehub-research.service tradehub-execution.service
python deploy/deployment_cli.py record --source-branch main --previous-commit <prior-sha>
python deploy/deployment_cli.py verify

# 5. live-host acceptance (restarts services deliberately)
.venv/bin/python deploy/fa06_acceptance.py
```

`record` refuses to stamp a manifest over **genuine** working-tree drift: a
deployment record must describe committed source. (`--allow-dirty` exists for
deliberate hotfix experiments and marks the manifest `tree_state: dirty`.)

## What `verify` checks

1. the manifest exists, parses, and validates (schema, SHAs, tree agreement);
2. `HEAD` **is** the recorded `deployed_commit`;
3. the working tree hash **is** the recorded `deployed_tree`;
4. there is **no genuine drift** (see below);
5. the recorded **rollback target exists** in this clone;
6. every declared **host-local configuration path is present**;
7. no superseded `DEPLOYED_COMMIT` marker has reappeared.

## Drift categories

`python deploy/deployment_cli.py drift` classifies every path git reports:

| Category | Examples | Verdict |
|---|---|---|
| **Committed source** | tracked files matching `HEAD` | expected |
| **Host-local** | `.env*`, `deployment.json`, `DEPLOYED_COMMIT` | expected, declared |
| **Generated/runtime** | `data/`, `.venv/`, `__pycache__/`, caches, `*.db`, `*.pk8` | expected, intentional |
| **Genuine drift** | any *other* modified/deleted/untracked path | **failure** |

Classification is declared in committed source, not inferred from `.gitignore`,
so it still holds if a `.gitignore` rule is edited.

## Rollback

The rollback target is the manifest's `previous_commit` — recorded at deploy
time, not hardcoded in a script.

```bash
python deploy/deployment_cli.py plan            # show from -> to, no action
python deploy/deployment_cli.py rollback --confirm
python deploy/deployment_cli.py verify
```

`rollback` checks the target exists, checks out that SHA **detached**, restarts
both services, and re-records the manifest with `previous_commit` set to the
revision it rolled back *from* — so a rollback is itself reversible and always
leaves a valid record. It requires `--confirm`; without it nothing happens.

## Ownership

The deployed checkout is owned by the **deploy user** (`jon`), and the documented
deploy path runs git as that user (`sudo -u jon git -C /opt/tiger-tradehub …`).
Running the deploy as root instead leaves root-owned files *and directories*
behind; a root-owned **directory** then blocks the next `git checkout` as `jon`
(git must create/unlink entries in it), so the next deploy fails halfway. Check:

```bash
sudo -u jon git ls-files | while read -r f; do
  [ "$(stat -c %U "$f")" = jon ] || echo "root-owned: $f"
done
```

A 2026-09-20 audit found mixed ownership (8 tracked files from the freshness
deploy owned by `root`, plus root-owned `deploy/`, `deploy/hermes/` and
`.git/refs/heads/fix/`, and a root-owned `.git/index`/`HEAD`/`config`); the tree
and its git metadata were normalised to `jon:jon`.

## Host-local files deliberately outside Git

| Path | Why |
|---|---|
| `/etc/tradehub/{execution,research,autonomy}.env` | credentials and tokens; never in Git, never recorded (paths only) |
| `/var/lib/tradehub`, `/var/lib/tradehub-research` | execution and research state |
| `/opt/tiger-tradehub/deployment.json` | the deployment record itself (gitignored) |
| `/opt/tiger-tradehub/.venv` | interpreter/venv for the bare-process units |
| `/var/lib/hermes/scripts/tradehub-health-watch.sh` | Hermes cron script, mirrored at `deploy/hermes/tradehub-health-watch.sh` and guarded byte-for-byte by `tests/test_deployment_provenance.py` |

## Seeing the whole picture

```
main  (authoritative branch)
  └── <deployed_commit>               ← the immutable revision production runs
        ├── detached checkout in /opt/tiger-tradehub, tree <deployed_tree>
        ├── previous_commit <sha>     ← rollback target
        └── record: deployment.json   ← written by deploy/deployment_cli.py record
```

Read the live values with `deployment_cli.py verify --json`; never from a
hand-maintained file.

## The committee model worker (`tradehub-committee-worker`)

The research cycle issues committee work envelopes; without a driver they simply
accumulate (`BLOCKED_NO_VALID_SCORE` -> zero proposals -> an idle runner), which is
exactly what happened between 2026-09-15 and 2026-09-25. `tradehub-committee-worker`
is the actuator that closes that loop, and nothing more:

```
research cycle -> committee queue -> worker -> assessments -> score snapshots
-> finalizer -> proposal / legitimate NO_ACTION -> runner
```

It replays the documented contract only: the work envelope and the submission go
through the authenticated committee API (whose router owns every retry, escalation
and scoring decision), and the artifact comes from the pinned MCP surface
(`get_evidence_pack`, fail-closed on a wrong or unpinned hash). It never queries
arbitrary evidence, never builds score snapshots, never calls the finalizer, never
touches eligibility or policy, and never processes the superseded historical backlog
(current genuine cycle only).

Install/enable (host-local, mirrors the other timers):

```bash
sudo install -m 0644 deploy/systemd/tradehub-committee-worker.service /etc/systemd/system/
sudo install -m 0644 deploy/systemd/tradehub-committee-worker.timer   /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now tradehub-committee-worker.timer
```

Operate and observe:

```bash
systemctl list-timers tradehub-committee-worker.timer
journalctl -u tradehub-committee-worker.service -n 50 -o cat
# one bounded invocation by hand (defaults: 4 runs, 12 model calls, 840 s)
sudo -u root bash -c 'set -a; . /etc/tradehub/research.env; set +a; \
  export TRADEHUB_RESEARCH_DIR=/var/lib/tradehub-research; \
  /opt/tiger-tradehub/.venv/bin/python -m tradehub_research.ops.committee_worker --dry-run'
# readiness only (no work claimed, no submissions)
... -m tradehub_research.ops.committee_worker --probe-only
```

The unit runs as `root` for one reason: the model runner CLI needs the Hermes install
under `/var/lib/hermes` (provider credentials, `0700 root:root`), and this host
already invokes model CLIs as root (Hermes cron jobs, the health watch). The worker
holds no trading authority — read-only DB access, submissions only through the
authenticated API, and it writes only its own state file.

Durable worker state lives in `$TRADEHUB_RESEARCH_DIR/committee-worker-state.json`
(last activity, provider readiness, last batch records, counters) and is read by the
health watch's worker-plane conditions (A: work exists but the worker never ran;
B: running but submissions are failing; C: assessments exist but the scorer or
finalizer is not advancing; D: a completed decision that legitimately recommended no
action is HEALTHY and is never alerted).

Role routes are provider-independent and configurable via
`RESEARCH_COMMITTEE_WORKER_ROUTES` (JSON override); the worker refuses to run when
neutral A/B or red-team/arbiter would share a provider. Providers are reached
through the configured runner CLI (`RESEARCH_COMMITTEE_WORKER_HERMES`, default
`hermes`), so no model credentials are added to the research plane.

