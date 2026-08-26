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
install -d -o root -g root -m 0755 /opt/tiger-tradehub
# Deploy the checkout and venv here, excluding all operator secret files.
# The service identities need read/execute access to this root-owned tree.
install -d -o tradehub-execution -g tradehub-execution -m 0750 /var/lib/tradehub
install -d -o tradehub-research -g tradehub-research -m 0750 /var/lib/tradehub-research
# Provision the files with explicit modes; do not rely on the caller's umask.
install -o root -g tradehub-execution -m 0640 /path/to/execution.env /etc/tradehub/execution.env
install -o root -g tradehub-research -m 0640 /path/to/research.env /etc/tradehub/research.env
install -o root -g tradehub-execution -m 0640 /path/to/tiger_private_key.pk8 /etc/tradehub/tiger_private_key.pk8
```

`/etc/tradehub/execution.env`; set `TRADEHUB_DATABASE_PATH=/var/lib/tradehub/tradehub.db` there. Store execution-only values
owner `root:tradehub-execution`. It contains `TRADEHUB_API_TOKEN`,
`TRADEHUB_PREVIEW_API_TOKEN`, and Tiger credential settings. Store the Tiger
private key at `/etc/tradehub/tiger_private_key.pk8`, mode `0640`, owner
`root:tradehub-execution`, and set `TIGEROPEN_PRIVATE_KEY_PATH` in the
execution environment file.

Store research-only values in `/etc/tradehub/research.env`, mode `0640`,
owner `root:tradehub-research`; set `RESEARCH_API_TOKEN` to a strong random research-only bearer and `RESEARCH_DB_PATH=/var/lib/tradehub-research/research.db` there. It must contain only `RESEARCH_*` settings and
must not contain execution or Tiger credentials.

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
