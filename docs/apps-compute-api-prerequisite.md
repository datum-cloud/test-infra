# Apps A1-T Compute API prerequisite (2026-10-20)

`components/compute` pins the real `datum-cloud/compute` source at commit
`88a7101c2b91f7195c2bb5433c50f54c384a90ce` and asks Flux to install
its `config/base/crd` bundle into the private Milo API. The source uses
the exact commit rather than a moving branch. Sparse checkout limits the
resulting artifact to the six CRD manifests. The
`compute-a1t-milo-api-crds` Kustomization waits for `milo-core` and uses the
test-only Milo kubeconfig already supplied by that baseline. It installs no
Compute operator, Workload, synthetic Kubernetes Deployment, or route.

The source tree at that commit renders exactly six `v1alpha` CRDs:
`InstanceConsoleSession`, `Instance`, `InstanceType`, `RuntimeClass`,
`WorkloadDeployment`, and `Workload`. The bounded read-only gate checks both
Flux revisions and generations, all six established CRDs in Milo, and Milo's
live API discovery. It requires explicit private host and Milo kubeconfigs:

```sh
python3 hack/check-compute-api-prerequisite.py \
  --host-kubeconfig /tmp/apps-a1t-kubeconfig \
  --host-context kind-apps-a1t-gateway-20261020 \
  --milo-kubeconfig /tmp/apps-a1t-milo-host-kubeconfig \
  --milo-context milo-root
```

This stage only establishes the native API schema. The next prerequisite is
the actual Compute management controller with a scoped Milo identity and an
explicit Karmada hub credential. Its upstream management overlay assumes a
co-located Karmada API, whereas the pinned PR32 topology has a separate hub.
The downstream DFW cell also needs its real controller, Locations
`ServingLocation`, and NSO's native network and edge resources before any
Apps A1-T Workload-to-route assertion is valid.

The private-cluster trial applied `components/compute` to the dedicated Milo
host. Flux recorded source artifact and Kustomization revision
`sha1:88a7101c2b91f7195c2bb5433c50f54c384a90ce` with
`Ready=True`; its inventory contained exactly six CRDs. The gate above exited
0 and confirmed all six CRDs were `Established=True` with `v1alpha` served by
Milo's live discovery. An initial source fetch timed out. The source
controller also timed out fetching Service Catalog during that period even
though the Git smart-HTTP endpoint was reachable from its pod. A diagnostic
restart with HTTP/2 disabled coincided with Compute's successful initial
fetch, but does not by itself establish the cause. That setting was removed;
the controller rolled out healthy with its original environment, and the
gate still passed afterward. A forced Flux source refresh under that restored
default environment then fetched the same exact revision successfully. A
commit-only fetch also completed under the restored environment, so the final
graph uses the durable commit-only pin and does not require an HTTP/2 override.
