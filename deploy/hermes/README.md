# Host-local Hermes deployment sources

Files here are the **canonical source** for scripts installed on the host outside
the TradeHub systemd estate. They live in Git so the server cannot drift from the
repository.

## `tradehub-health-watch.sh`

| | |
|---|---|
| Canonical source | `deploy/hermes/tradehub-health-watch.sh` (this file's sibling) |
| Installed to | `/var/lib/hermes/scripts/tradehub-health-watch.sh` |
| Mode | `0755 root:root` |
| Driven by | Hermes cron job `TradeHub health watch` (`no_agent`, daily 01:00, stdout delivered verbatim) |
| Runs as | `tradehub-research` (via `runuser`), with `/etc/tradehub/research.env` |
| Exit contract | `0` for any run that completed (including a degraded report); non-zero only for a genuine checker crash |

### Deployment

```bash
install -m 0755 -o root -g root \
  /opt/tiger-tradehub/deploy/hermes/tradehub-health-watch.sh \
  /var/lib/hermes/scripts/tradehub-health-watch.sh
```

### Anti-drift

`tests/test_deployment_provenance.py::TestInstalledHostScriptsHaveCommittedSources`
compares the installed copy byte-for-byte against this source and **fails** on a
difference. It skips when the installed path is absent (e.g. CI), so it guards
the host without breaking the pipeline. The same module also proves the source
file is tracked by git, so the script cannot exist on the host with no committed
origin.

If the guard fails, the server has been edited directly: reinstall from this
source rather than hand-patching the host copy.
