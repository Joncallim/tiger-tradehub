# Hearth runtime isolation

These units implement the minimal #45/#39 runtime-isolation subset. They are
intended for the Hearth bare-process deployment; they do not replace the rest
of #39.

## Provisioning

Run as root on the host:

```bash
install -d -o root -g root -m 0751 /etc/tradehub
useradd --system --home-dir /nonexistent --shell /usr/sbin/nologin tradehub-execution
useradd --system --home-dir /nonexistent --shell /usr/sbin/nologin tradehub-research
useradd --system --home-dir /nonexistent --shell /usr/sbin/nologin tradehub-autonomy
install -d -o root -g root -m 0755 /opt/tiger-tradehub
# Deploy the checkout and venv here, excluding all operator secret files.
# The service identities need read/execute access to this root-owned tree.
install -d -o tradehub-execution -g tradehub-execution -m 0750 /var/lib/tradehub
install -d -o tradehub-research -g tradehub-research -m 0750 /var/lib/tradehub-research
# Setgid handoff: execution owns writes; research group has read-only access.
install -d -o tradehub-execution -g tradehub-research -m 2750 /var/lib/tradehub-research/handoff
install -d -o tradehub-execution -g tradehub-autonomy -m 0750 /var/lib/tradehub/autonomy
install -d -o tradehub-autonomy -g tradehub-autonomy -m 0770 /var/lib/tradehub/autonomy/proposals
install -d -o tradehub-autonomy -g tradehub-autonomy -m 0750 /var/lib/tradehub-research/autonomy
# Execution writes the handoff and autonomy reads its own state, but each only
# traverses the research-owned parent (no listing/read of research contents).
setfacl -m u:tradehub-execution:--x,u:tradehub-autonomy:--x /var/lib/tradehub-research
# The research exporter can traverse the execution-owned parents and create
# proposal envelopes, but cannot list/change either parent or read/change the
# execution-owned kill switch.  Both parent traversal ACLs are required.
setfacl -m u:tradehub-research:--x /var/lib/tradehub /var/lib/tradehub/autonomy
setfacl -m u:tradehub-research:rwx,m::rwx /var/lib/tradehub/autonomy/proposals
# Research-created envelopes must remain readable by the autonomy runner even
# under a restrictive umask.  The exporter also creates files mode 0640.
setfacl -m d:u:tradehub-autonomy:r-x,d:m::rwx /var/lib/tradehub/autonomy/proposals
# Provision the files with explicit modes; do not rely on the caller's umask.
install -o root -g tradehub-execution -m 0640 /path/to/execution.env /etc/tradehub/execution.env
install -o root -g tradehub-research -m 0640 /path/to/research.env /etc/tradehub/research.env
install -o root -g tradehub-autonomy -m 0640 /path/to/autonomy.env /etc/tradehub/autonomy.env
install -o root -g tradehub-execution -m 0640 /path/to/tiger_private_key.pk8 /etc/tradehub/tiger_private_key.pk8
```

`/etc/tradehub/execution.env`; set `TRADEHUB_DATABASE_PATH=/var/lib/tradehub/tradehub.db` there. Store execution-only values
owner `root:tradehub-execution`. It contains `TRADEHUB_API_TOKEN`,
`TRADEHUB_PREVIEW_API_TOKEN`, and Tiger credential settings. Store the Tiger
private key at `/etc/tradehub/tiger_private_key.pk8`, mode `0640`, owner
`root:tradehub-execution`, and set `TIGEROPEN_PRIVATE_KEY_PATH` in the
execution environment file.

Store research-only values in `/etc/tradehub/research.env`, mode `0640`,
owner `root:tradehub-research`; set `RESEARCH_API_TOKEN` to a strong random research-only bearer and `RESEARCH_DB_PATH=/var/lib/tradehub-research/research.db` there. It must contain only `RESEARCH_*` settings and must not contain execution or Tiger credentials.

Set `TRADEHUB_DRY_RUN=true` in `/etc/tradehub/autonomy.env`, the environment
read by the only process that calls the guarded execution API.  This is the
authoritative execution-boundary setting; a value in `research.env` has no
broker-write effect.

Install and enable the units:

```bash
install -m 0644 tradehub-execution.service tradehub-research.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now tradehub-execution.service tradehub-research.service
```

## Structural invariant

`tradehub-research` runs as `tradehub-research`, not root and not
`tradehub-execution`. Its unit hides the execution environment and private-key
paths with `InaccessiblePaths`, and `ProtectHome=true` prevents traversal of
operator home files. The execution unit is the only unit granted the
`tradehub-execution` group that can read `/etc/tradehub/execution.env` and the
Tiger key. A compromised research process therefore cannot obtain the
execution bearer, preview capability, Tiger account credentials, or private
key from the intended deployment filesystem.

The units deliberately leave the broader deployment/runtime work in #39 open.

## Decision-pipeline deployment

The M/W/F cycle must use the same `/var/lib/tradehub-research/research.db` as
the committee API. Do not point any research unit at an operator home checkout.
The cycle creates real committee work, persists only completed committee scores,
and invokes the portfolio engine with the labelled `paper-provisional-v1`
policy. Missing sanitized broker state remains an explicit fail-closed
no-action result. The paper runner consumes only equality-checked envelopes in
`/var/lib/tradehub/autonomy/proposals`; it remains PAPER plus dry-run. The
autonomy bearer belongs only in `/etc/tradehub/autonomy.env` (readable only by
`tradehub-autonomy`); the committee/research service never receives it. Keep
the kill switch execution-owned. The runner receives only read access to it.

Install the decision services and timers together; the finalizer is what
continues committee work completed after the M/W/F cycle returns:

```bash
install -m 0644 deploy/systemd/tradehub-research-cycle.service deploy/systemd/tradehub-research-cycle.timer /etc/systemd/system/
install -m 0644 deploy/systemd/tradehub-committee-finalizer.service deploy/systemd/tradehub-committee-finalizer.timer /etc/systemd/system/
install -m 0644 deploy/systemd/tradehub-reconcile.service deploy/systemd/tradehub-reconcile.timer /etc/systemd/system/
install -m 0644 deploy/systemd/tradehub-paper-autonomy.service deploy/systemd/tradehub-paper-autonomy.timer /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now tradehub-reconcile.timer tradehub-research-cycle.timer tradehub-committee-finalizer.timer tradehub-paper-autonomy.timer
```

Before enabling a production timer, prove the loaded service identity and the
actual filesystem boundary (these probes create only a hidden non-envelope
file and do not contact a broker):

```bash
systemctl show tradehub-research-cycle.service tradehub-committee-finalizer.service -p User -p Group -p ReadWritePaths -p InaccessiblePaths -p ProtectSystem -p ProtectHome
probe=/var/lib/tradehub/autonomy/proposals/.acl-probe.$$
runuser -u tradehub-research -- sh -ceu 'p="$1"; test -x /var/lib/tradehub; test -x /var/lib/tradehub/autonomy; test -w /var/lib/tradehub/autonomy/proposals; : > "$p"; chmod 0640 "$p"; test ! -r /var/lib/tradehub/autonomy/kill_switch; test ! -w /var/lib/tradehub/autonomy/kill_switch; test ! -r /etc/tradehub/execution.env; test ! -r /etc/tradehub/autonomy.env' sh "$probe"
runuser -u tradehub-autonomy -- test -r "$probe"
rm -f "$probe"
grep -qx 'TRADEHUB_DRY_RUN=true' /etc/tradehub/autonomy.env
```

The last check is mandatory execution-boundary evidence. It is not satisfied
by a research environment setting or by a dry-run value merely documented in
source control.
