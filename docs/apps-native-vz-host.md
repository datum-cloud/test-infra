# Dedicated Colima VZ host bootstrap

This host bootstrap moves the script used for the 2026-10-21 Colima host into
its owning test-infra repository. It consumes this checkout's existing Kind,
Flux, cert-manager, Kyverno, Gateway and Milo bootstrap sources. It creates no
Apps-owned deployment copy. It preserves the selected global Docker and
kubectl contexts by using a process-local Docker endpoint and private
kubeconfig.

Use an explicitly named ARM Colima VZ profile created for this system, with
nested virtualization, at least six CPUs and 16 GiB memory. On a compatible
Apple Silicon host the verified Colima 0.10.1 command shape is:

```sh
colima start apps-native-vz-NEWNAME --vm-type vz --nested-virtualization \
  --cpu 6 --memory 16 --disk 40 --runtime docker \
  --activate=false --ssh-config=false
```

Choose a new profile name; do not reuse an unrelated profile. The host helper
does not create, start, stop, delete or resize Colima itself. It requires the
selected profile's Docker socket and ARM daemon, six CPUs, a KVM character
device, no existing Kind clusters, and three free local ports. KVM device
presence is a prerequisite, not proof of a working Compute guest.

From a clean signed test-infra checkout, run the read-only preflight:

```sh
bash hack/bootstrap-apps-native-vz-host.sh \
  --profile apps-native-vz-NEWNAME \
  --cluster apps-a1t-ci-vz-NEWNAME \
  --private-dir /private/tmp/apps-native-vz-NEWNAME \
  --port-base 33000
```

Names must use lowercase letters, digits and hyphens after the required prefix;
replace `NEWNAME` in the example with such a name. The base port is Grafana's
host port, base+80 is Gateway HTTP, and base+443 is Gateway HTTPS. The default
base is 32000. Choose free ports when preserving another running fixture.

Repeat the same command with `--execute` only for the new dedicated host. It
verifies the source commit signature and records its revision in a private, flushed creation intent before
copying the audit policy or creating the Kind cluster. Signature verification is not independent approval of that revision. The audit policy goes into a new random private directory owned by the VM user, without a privileged write or reuse of a shared temporary file. Kind uses the immutable
ARM-compatible node index already verified by the historical trial. Every
Kind config source substitution must match exactly once. Source manifests
supply the full baseline, and the existing Milo Flux runner supplies its
readiness and diagnostics. All credential files remain in the private artifact
directory; never publish that directory.

The helper refuses an existing evidence path or any existing Kind cluster in
the selected VM. A failed cluster census also fails closed. It never adopts,
resets, or automatically deletes a cluster. Partial failures intentionally
retain the selected host and diagnostics; recovery must use its recorded
identity and the owning fixture, not deletion based on a name alone. The
bootstrap does not create the separate Kata member cluster, install the guest
runtime, or prove Workload traffic.

The historical fixed-name script created the currently running VZ host. This
repository version adds explicit names, port selection, read-only default,
clean-source enforcement and durable intent. Its offline rejection tests are
new evidence; a fresh complete bootstrap with this revised script has not yet
been run. Do not equate the historical success with a new full environment
pass.

Independent review corrected the audit copy to avoid privileged writes through
predictable temporary paths. Seven focused tests now include the real local
shell payload for private-directory creation and preservation of an old symlink.
The full offline suite passes 142 tests. A read-only preflight against the
existing dedicated VZ VM refused its existing Kind cluster and created no
artifact directory; no execute-mode replay was attempted.
