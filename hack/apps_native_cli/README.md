# Native Apps command probe

This helper builds the exported `datumctl` Apps command from clean commit
`91da75c3123282aac7ba276f120ebf93d2a80177` in a disposable private clone.
It accepts one mode-0600 kubeconfig for the existing customer identity. The
selected server must be exactly `https://127.0.0.1:<port>/apis/resourcemanager.miloapis.com/v1alpha1/projects/<project>/control-plane`.
The binary has no token argument. Its scoped config and build clone stay in the
main fixture's mode-0700 temporary directory and are removed with it.

Pass `--datumctl-source /absolute/clean/datumctl-checkout` to the native Apps
trial to enable this optional path. It builds the private binary before any
cluster preflight. After the main trial has acknowledged sequence 2, it creates
a Project-path kubeconfig using its existing customer token and verified public
Milo CA, then exercises history, diff, server dry-run restore and actual restore
to sequence 3. Its first UID/RV delete uses the exported CLI; the main trial
still proves retained history and same-name replacement. Without the option,
the original API-only path remains in use. The equivalent helper interface is:

```python
from apps_native_cli.driver import (AppsCLI, build_private_cli,
                                    exercise_history_diff_restore, delete_with_cli)

binary = build_private_cli(pinned_cli_source, private)
cli = AppsCLI(binary, private / "customer-project.kubeconfig", project, milo_port)
# snapshot() returns {"template": full API object or None,
#                     "revisions": all full Project API revision items}.
restored = exercise_history_diff_restore(cli, snapshot, wait_sequence)
# Pass restored to the main history/deletion assertions. When ready to delete:
deleted = delete_with_cli(cli, snapshot, wait_deleted)
```

`wait_sequence(3)` must wait for native controller status acknowledgment and
`wait_deleted()` must wait for a real 404. The callbacks use existing Project
API requests; the helper never creates a second identity or cluster context.
The helper compares every persisted template and revision field after each
read or server dry-run, including metadata resource versions and managed
fields. It normalizes only the order of returned revision items. A failure
retains the main fixture's normal fail-closed cleanup path.

This test-only entry point bypasses datumctl's interactive root OAuth factory
by calling its exported Apps command with the scoped kubeconfig. It proves the
command's real Project API HTTP behavior under the local static-RBAC fixture,
not production OAuth or IAM. No live run is claimed by these files alone.
