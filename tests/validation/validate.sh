#!/bin/bash

set -euo pipefail

echo "🔍 Validating test-infra repository structure and functionality"
echo "=============================================================="

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Validation functions
validate_file() {
    local file="$1"
    local description="$2"

    if [[ -f "$file" ]]; then
        echo -e "  ✅ ${GREEN}$description${NC}"
        return 0
    else
        echo -e "  ❌ ${RED}$description${NC}"
        return 1
    fi
}

validate_directory() {
    local dir="$1"
    local description="$2"

    if [[ -d "$dir" ]]; then
        echo -e "  ✅ ${GREEN}$description${NC}"
        return 0
    else
        echo -e "  ❌ ${RED}$description${NC}"
        return 1
    fi
}

validate_yaml() {
    local file="$1"
    local description="$2"

    if [[ -f "$file" ]]; then
        if command -v yq >/dev/null 2>&1; then
            if yq eval '.' "$file" >/dev/null 2>&1; then
                echo -e "  ✅ ${GREEN}$description (valid YAML)${NC}"
                return 0
            else
                echo -e "  ❌ ${RED}$description (invalid YAML)${NC}"
                return 1
            fi
        else
            echo -e "  ⚠️  ${YELLOW}$description (yq not available, skipping YAML validation)${NC}"
            return 0
        fi
    else
        echo -e "  ❌ ${RED}$description (file not found)${NC}"
        return 1
    fi
}

# Start validation
echo ""
echo "📁 Repository Structure"
echo "======================"

# Core directories
validate_directory "actions" "Actions directory"
validate_directory "actions/setup" "Setup action directory"
validate_directory "actions/cleanup" "Cleanup action directory"
validate_directory "container" "Container directory"
validate_directory "config" "Config directory"
validate_directory "docs" "Documentation directory"
validate_directory "examples" "Examples directory"
validate_directory "tests" "Tests directory"

echo ""
echo "📄 Core Files"
echo "============="

# Root files
validate_file "README.md" "Root README"
validate_file "LICENSE" "License file"
validate_file ".gitignore" "Git ignore file"

# Action files
validate_yaml "actions/setup/action.yml" "Setup action definition"
validate_yaml "actions/cleanup/action.yml" "Cleanup action definition"
validate_file "actions/setup/README.md" "Setup action README"
validate_file "actions/cleanup/README.md" "Cleanup action README"

# Container files
validate_file "container/Dockerfile" "Container Dockerfile"
validate_file "container/entrypoint.sh" "Container entrypoint"
validate_directory "container/scripts" "Container scripts directory"
validate_file "container/scripts/install-components.sh" "Component installation script"
validate_file "container/scripts/install-flux-direct.sh" "Flux installation script"

# Config files
validate_yaml "config/kustomization.yaml" "Main kustomization"
validate_directory "config/cert-manager" "cert-manager config directory"
validate_yaml "config/cert-manager/kustomization.yaml" "cert-manager kustomization"
validate_yaml "config/cert-manager/helmrepository.yaml" "cert-manager HelmRepository"
validate_yaml "config/cert-manager/helmrelease.yaml" "cert-manager HelmRelease"
validate_yaml "config/cert-manager/clusterissuers.yaml" "cert-manager ClusterIssuers"

# Documentation files
validate_file "docs/README.md" "Main documentation"
validate_file "docs/CONFIGURATION.md" "Configuration documentation"
validate_file "docs/CONTRIBUTING.md" "Contributing guide"
validate_file "docs/TROUBLESHOOTING.md" "Troubleshooting guide"

# GitHub workflow
validate_yaml ".github/workflows/build-image.yml" "Build workflow"

echo ""
echo "🔧 Script Validation"
echo "==================="

# Check script permissions
if [[ -x "container/scripts/install-components.sh" ]]; then
    echo -e "  ✅ ${GREEN}install-components.sh is executable${NC}"
else
    echo -e "  ❌ ${RED}install-components.sh is not executable${NC}"
fi

if [[ -x "container/scripts/install-flux-direct.sh" ]]; then
    echo -e "  ✅ ${GREEN}install-flux-direct.sh is executable${NC}"
else
    echo -e "  ❌ ${RED}install-flux-direct.sh is not executable${NC}"
fi

if [[ -x "container/entrypoint.sh" ]]; then
    echo -e "  ✅ ${GREEN}entrypoint.sh is executable${NC}"
else
    echo -e "  ❌ ${RED}entrypoint.sh is not executable${NC}"
fi

echo ""
echo "📦 Kustomize Validation"
echo "======================="

if command -v kustomize >/dev/null 2>&1; then
    if kustomize build config/ >/dev/null 2>&1; then
        echo -e "  ✅ ${GREEN}Main kustomization builds successfully${NC}"
    else
        echo -e "  ❌ ${RED}Main kustomization build failed${NC}"
    fi

    if kustomize build config/cert-manager/ >/dev/null 2>&1; then
        echo -e "  ✅ ${GREEN}cert-manager kustomization builds successfully${NC}"
    else
        echo -e "  ❌ ${RED}cert-manager kustomization build failed${NC}"
    fi
else
    echo -e "  ⚠️  ${YELLOW}kustomize not available, skipping build validation${NC}"
fi

echo ""
echo "🎯 Action Reference Validation"
echo "=============================="

# Check for correct action references in examples
if grep -q "datum-cloud/test-infra/actions/setup@main" examples/*.yml; then
    echo -e "  ✅ ${GREEN}Examples use correct setup action path${NC}"
else
    echo -e "  ❌ ${RED}Examples don't use correct setup action path${NC}"
fi

if grep -q "datum-cloud/test-infra/actions/cleanup@main" examples/*.yml; then
    echo -e "  ✅ ${GREEN}Examples use correct cleanup action path${NC}"
else
    echo -e "  ❌ ${RED}Examples don't use correct cleanup action path${NC}"
fi

echo ""
echo "📊 Repository Statistics"
echo "======================="

echo "  📁 Total directories: $(find . -type d -not -path "./.git*" | wc -l)"
echo "  📄 Total files: $(find . -type f -not -path "./.git*" | wc -l)"
echo "  📝 YAML files: $(find . -name "*.yml" -o -name "*.yaml" | wc -l)"
echo "  🐚 Shell scripts: $(find . -name "*.sh" | wc -l)"
echo "  📚 Documentation files: $(find docs/ -name "*.md" | wc -l)"

echo ""
echo "✅ Validation complete!"
echo "======================"
