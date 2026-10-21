# Exact Catalog source delivery after the Go-Git timeout

The existing task-owned `service-catalog` GitRepository still names commit
`5f2b663b6c5e039c4c7b666ef3e959803851b675` and retains its original UID.
Its last artifact has that revision, but current fetches fail after the 60-second
deadline. A same-network, same-implementation Go-Git clone timed out at 300
seconds after receiving 221,040 KiB. A shallow CLI commit fetch completed in
under one second, but is not the code path used by the pinned Flux controller.
Do not raise the source timeout or regard the shallow fetch as a Flux fix.

The upstream publication workflow at this Catalog source revision publishes
`config/` as `ghcr.io/milo-os/service-catalog-kustomize`. Tag
`v0.0.0-5f2b663` has immutable OCI manifest digest
`sha256:d34680228b91a5a5d46fedb40700f805b9b2ce97f13232e178cffa2db2cae431`.
The manifest annotation identifies the full commit and source repository. Its
single gzip layer digest is
`sha256:45e7a0d1afb367bc2e33ba08b2e01dac96887d46ba74f90ec3139a0ebc42d6af`.
Of 89 files, 88 match the clean source `config/` byte-for-byte. The only changed
file, `base/manager/kustomization.yaml`, replaces the source's `latest` image
tag with the publisher's `v0.14.5` tag. No API schema or webhook source file
changed. With the source root adjusted from `config/` to the OCI bundle root,
the current API stage and proposed OCI stage each render the same 13 Kubernetes
objects; canonical render SHA256 is
`a861cfd2310f7bed584ef358abcd82083243615efd704656114603455d3c1f41`.

Implement a separate, private UID journal for this transition. Before writes,
verify the clean full Catalog commit, exact manifest and layer hashes, full
source-tree equivalence with only the known publisher image substitution, both
13-object renders, the original Catalog graph journal, the exact existing
GitRepository UID/spec/artifact revision, and the API Kustomization UID/spec,
last-applied revision and 13-entry inventory. Create one new OCIRepository in
`flux-system` using the immutable digest and record its UID. Wait for its exact
digest artifact. Change only the existing API Kustomization's `sourceRef` and
`path` through a UID/resourceVersion/spec-tested JSON patch; never create a
second Kustomization for those APIs. Record patch intent before the write, then
require Ready, the exact OCI revision, and the unchanged 13-entry inventory.
Retain the original GitRepository unchanged as provenance and for recovery.

The dependent LocationBinding stage must accept the verified OCI state only
with this transition journal: same host/root identity, original Git and API
UIDs, new OCI UID and digest, exact API target spec, and current Ready/inventory.
It must not treat the old failed Git source as Ready. For cleanup, retain the
OCI source and remote API stage while LocationBinding, Locations, Projects,
Catalog operator or Catalog data depend on them. After those are absent and
full source-owned census succeeds, prune the **existing** API stage against its
OCI contract, verify its UID and remote API absence, then remove the new OCI
source by UID/resourceVersion. The original Catalog graph cleanup can then
remove Billing and the retained Git source. A failed or ambiguous transition
retains both source capabilities and the journal; a fresh GET may resolve an
uncertain patch only if it matches the exact old or new spec and original UID.
Never delete a source while a live Kustomization may still fetch from it.

These checks are not an atomic lock across Flux and Milo. They require the
exclusive task-owned environment. No Catalog operator, entitlement, customer
install, or Workload readiness follows from the source transition alone.
