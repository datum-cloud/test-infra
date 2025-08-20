# Observability Component

Optional observability stack for test infrastructure, providing metrics, logs, and distributed tracing.

## Overview

This component is an **optional add-on** deployed after core infrastructure. It provides comprehensive telemetry capabilities using Grafana, Victoria Metrics, Loki, and Tempo.

## Components

- **Grafana**: Visualization and dashboards
- **Victoria Metrics**: Metrics collection and storage
- **Loki**: Log aggregation and storage
- **Tempo**: Distributed tracing storage
- **Promtail**: Log collection agent

## Access

- **Grafana UI**: Available at NodePort 30000 (admin/datum123)
- **Default Datasources**: Victoria Metrics (metrics), Loki (logs), Tempo (traces)

## Prerequisites

Core test infrastructure must be running before deploying observability components.

## Deployment

Deploy using the `install-observability` task target:

```bash
task install-observability
```

This deploys all components and configures datasources automatically.

## Removal

To remove the observability stack:

```bash
kubectl delete namespace observability
```
