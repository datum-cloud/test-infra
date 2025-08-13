#------------------------------------------------------------------------------
# globals
#------------------------------------------------------------------------------
CLUSTER_NAME ?= test-infra
K8S_VERSION  ?= v1.30.0         # change to taste
KIND_CFG     := cluster/kind-config.yaml
TOOLS        := kind kubectl kustomize flux

# --- NEW: explicit tool versions ------------------------------------------------
KIND_VERSION      ?= v0.29.0

# make the pins visible to any child script (set -u safe)
export KIND_VERSION 
# -----------------------------------------------------------------------------

SHELL := /usr/bin/env bash

# --------------------------
#       Configuration
# --------------------------
WAIT_TIMEOUT ?= 300s   # how long to wait for add-ons to become ready

#------------------------------------------------------------------------------
# high-level targets
#------------------------------------------------------------------------------
.PHONY: cluster-up
cluster-up: ensure-tools create-kind install-components  ## spin everything up

.PHONY: cluster-down
cluster-down: delete-kind                                ## delete the cluster

.PHONY: help
help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?##' $(MAKEFILE_LIST) | \
	  awk 'BEGIN {FS = ":.*?##"}; {printf "%-25s %s\n", $$1, $$2}'

#------------------------------------------------------------------------------
# granular targets
#------------------------------------------------------------------------------
.PHONY: ensure-tools
ensure-tools: hack/ensure-tools.sh                       ## install/verify deps
	@bash $< $(TOOLS)

.PHONY: create-kind
create-kind: ## create KIND cluster
	@echo "➡️  Creating KIND cluster '$(CLUSTER_NAME)' ..."
	@kind get clusters | grep -q "^$(CLUSTER_NAME)$$" || \
	  kind create cluster --name $(CLUSTER_NAME) --image kindest/node:$(K8S_VERSION) --config $(KIND_CFG)

.PHONY: delete-kind
delete-kind: ## delete KIND cluster
	@echo "🗑️  Deleting KIND cluster '$(CLUSTER_NAME)' ..."
	@kind delete cluster --name $(CLUSTER_NAME) || true

.PHONY: install-components
install-components: install-flux install-cert-manager install-kyverno install-envoy-gateway-operator ## all add-ons
	@echo "➡️  Installed cluster components ..."

.PHONY: install-cert-manager
install-cert-manager:   ## deploy cert-manager (+ CSI driver) via Kustomize, then wait
	@echo "➡️  Reconciling cert-manager …"
	kustomize build components/cert-manager | kubectl apply -f -
	@echo "⏳ Waiting for cert-manager HelmReleases …"
	@kubectl -n cert-manager wait helmrelease/{cert-manager,cert-manager-csi-driver} \
	    --for=condition=Ready --timeout=$(WAIT_TIMEOUT)
	@echo "✅ cert-manager and CSI driver are ready"

.PHONY: install-flux
install-flux:        ## deploy Flux via Kustomize, then wait for its controllers
	@echo "➡️  Reconciling Flux …"
	kustomize build components/flux | kubectl apply -f -
	@echo "⏳ Waiting for Flux controllers …"
	@kubectl -n flux-system wait deployment/{source-controller,helm-controller,kustomize-controller,notification-controller} \
	    --for=condition=Available --timeout=$(WAIT_TIMEOUT)
	@echo "✅ Flux is ready"

.PHONY: install-kyverno
install-kyverno:     ## deploy or upgrade Kyverno idempotently, then wait
	@echo "➡️  Reconciling Kyverno …"
	@kustomize build components/kyverno | \
	  kubectl apply --server-side --field-manager=kyverno-installer --force-conflicts -f -
	@echo "⏳ Waiting for Kyverno controllers …"
	@kubectl -n kyverno wait deployment/kyverno-{admission-controller,background-controller,cleanup-controller,reports-controller} \
	    --for=condition=Available --timeout=$(WAIT_TIMEOUT)
	@echo "✅ Kyverno is ready"

.PHONY: install-envoy-gateway-operator
install-envoy-gateway-operator:     ## deploy Envoy Gateway Operator via Kustomize, then wait
	@echo "➡️  Reconciling Envoy Gateway Operator …"
	kustomize build components/envoy-gateway-operator | kubectl apply -f -
	@echo "⏳ Waiting for Envoy Gateway Operator HelmRelease …"
	@kubectl -n flux-system wait helmrelease/envoy-gateway \
	    --for=condition=Ready --timeout=$(WAIT_TIMEOUT)
	@echo "✅ Envoy Gateway Operator is ready"

# ------------------------------------------------------------------
# Helpers for CI
# ------------------------------------------------------------------
.PHONY: kind-load-image   ## Load one or more images into KIND
kind-load-image:
	@if [ -z "$(IMAGES)" ]; then \
		echo "ERROR: pass IMAGES=\"img1 img2 ...\""; exit 1; \
	fi
	@for img in $(IMAGES); do \
		echo "➡️  Loading $$img into kind '$(CLUSTER_NAME)'"; \
		docker pull $$img || true; \
		kind load docker-image $$img --name $(CLUSTER_NAME); \
	done

.PHONY: kind-save-image   ## Save an image tarball artefact
kind-save-image:
	@if [ -z "$(IMAGE)" ] || [ -z "$(TAR)" ]; then \
		echo "ERROR: set IMAGE=<name> and TAR=<file>"; exit 1; \
	fi
	@docker save "$(IMAGE)" | gzip > "$(TAR)"