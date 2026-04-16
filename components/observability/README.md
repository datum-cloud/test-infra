# Observability Component

Optional observability stack for test infrastructure, providing metrics, logs, and distributed tracing.

## Overview

This component is an **optional add-on** deployed after core infrastructure. It provides comprehensive telemetry capabilities using Grafana, Victoria Metrics, Loki, Tempo, and the OpenTelemetry Collector.

The stack is split into composable subcomponents — each deploys into its own namespace so it can be installed, upgraded, or removed independently (`kubectl delete ns <component>-system` cleanly uninstalls).

## Subcomponents

| Component         | Namespace                 | Purpose                               |
| ----------------- | ------------------------- | ------------------------------------- |
| `prometheus-crds` | (cluster-scoped)          | Prometheus Operator CRDs              |
| `victoria-metrics`| `victoria-metrics-system` | Metrics collection and storage        |
| `otel-collector`  | `otel-collector-system`   | OpenTelemetry DaemonSet collector     |
| `loki`            | `loki-system`             | Log aggregation and storage           |
| `tempo`           | `tempo-system`            | Distributed tracing storage           |
| `grafana`         | `grafana-system`          | Visualization, dashboards, datasources|

## Access

- **Grafana UI**: Available at NodePort 30000 (admin/datum123)
- **Default Datasources**: Victoria Metrics (metrics), Loki (logs), Tempo (traces), Alertmanager

## Prerequisites

Core test infrastructure must be running before deploying observability components.

## Deployment

Deploy the whole stack:

```bash
task install-observability
```

Or install components individually:

```bash
task install-prometheus-crds
task install-victoria-metrics
task install-otel-collector
task install-loki
task install-tempo
task install-grafana
```

## Removal

To remove an individual component:

```bash
kubectl delete namespace <component>-system
```

To remove the whole stack:

```bash
for ns in grafana-system tempo-system loki-system otel-collector-system victoria-metrics-system; do
  kubectl delete namespace "$ns"
done
```
