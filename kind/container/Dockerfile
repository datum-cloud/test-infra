# Custom Kind Image with Flux and cert-manager
FROM kindest/node:v1.28.0

# Install dependencies
USER root

# Install curl, jq, and other utilities
RUN apt-get update && apt-get install -y \
    curl \
    jq \
    wget \
    unzip \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Install kubectl
RUN curl -LO "https://dl.k8s.io/release/$(curl -L -s https://dl.k8s.io/release/stable.txt)/bin/linux/amd64/kubectl" \
    && chmod +x kubectl \
    && mv kubectl /usr/local/bin/

# Install flux CLI
RUN curl -s https://fluxcd.io/install.sh | bash \
    && mv /root/.local/bin/flux /usr/local/bin/

# Install Helm
RUN curl https://raw.githubusercontent.com/helm/helm/main/scripts/get-helm-3 | bash

# Install kustomize
RUN curl -s "https://raw.githubusercontent.com/kubernetes-sigs/kustomize/master/hack/install_kustomize.sh" | bash \
    && mv kustomize /usr/local/bin/

# Copy installation scripts
COPY scripts/ /kind/scripts/
RUN chmod +x /kind/scripts/*.sh

# Copy configuration files and kustomization manifests
COPY config/ /kind/config/

# Set up the entrypoint script
COPY container/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# Switch back to kind user
USER kind

# Use custom entrypoint
ENTRYPOINT ["/entrypoint.sh"]
