# Envoy Gateway Operator

Provides HTTPS ingress using Kubernetes Gateway API with merged gateway
architecture for efficient resource usage in test infrastructure. The envoy
gateway project implements the [Kubernetes Gateway
API](https://gateway-api.sigs.k8s.io/) using Envoy.

Features provided by the Gateway API:

- **Standard ingress interface** - Vendor-neutral way to configure ingress
  controllers
- **HTTPRoute resources** - Declarative HTTP routing with path/header matching
- **TLS termination** - Automatic certificate management via cert-manager
- **Cross-namespace routing** - Routes can reference services in different
  namespaces

Unlike traditional Ingress controllers, Gateway API separates infrastructure
(Gateway) from routing configuration (HTTPRoute), enabling better multi-tenancy
and role separation.

## Quick Start

```bash
# Deploy with full infrastructure
task cluster-up  # Automatically includes Envoy Gateway

# Or deploy component independently
task install-envoy-gateway-operator
```

## What It Provides

- **Single HTTPS gateway** handling all ingress traffic on port 8443
- **Cross-namespace routing** - HTTPRoutes can be created in any namespace
- **Self-signed TLS** via cert-manager for development
- **Non-privileged ports** (8443) - no admin privileges required

## Usage

Create HTTPRoutes that reference the shared gateway:

```yaml
apiVersion: gateway.networking.k8s.io/v1
kind: HTTPRoute
metadata:
  name: my-app-route
spec:
  parentRefs:
    - name: default-gateway
      namespace: envoy-gateway-system
  hostnames:
    - "my-app.localhost"
  rules:
    - backendRefs:
        - name: my-service
          port: 80
```

Test with:
```bash
curl -k -H "Host: my-app.localhost" https://localhost:8443/
```

## Troubleshooting

```bash
# Check gateway status
kubectl get gateway default-gateway -n envoy-gateway-system

# Check HTTPRoute status
kubectl get httproute -A

# View logs
kubectl logs -n envoy-gateway-system -l app.kubernetes.io/name=envoy-gateway
```
