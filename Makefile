#------------------------------------------------------------------------------
# globals
#------------------------------------------------------------------------------
CLUSTER_NAME ?= test-infra
K8S_VERSION  ?= v1.30.0         # change to taste
KIND_CFG     := cluster/kind-config.yaml
TOOLS        := kind kubectl kustomize flux

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
install-components: install-cert-manager install-flux install-kyverno wait-ready ## all add-ons
	@echo "➡️  Installing cluster components ..."

.PHONY: install-cert-manager
install-cert-manager: ## deploy cert-manager via Kustomize
	@echo "➡️  Reconciling cert-manager ..."
	kustomize build components/cert-manager | kubectl apply -f -

.PHONY: install-flux
install-flux: ## deploy Flux via Kustomize
	@echo "➡️  Reconciling Flux ..."
	kustomize build components/flux | kubectl apply -f -

.PHONY: install-kyverno
install-kyverno:  ## Deploy or upgrade Kyverno idempotently
	@echo "➡️  Reconciling Kyverno ..."
	@kustomize build components/kyverno | kubectl apply --server-side --field-manager=kyverno-installer --force-conflicts -f -

.PHONY: wait-ready
wait-ready:              ## Wait for all deployments, show progress
	@./hack/wait-ready.sh $(WAIT_TIMEOUT)
