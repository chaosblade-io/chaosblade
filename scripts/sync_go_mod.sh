#!/bin/bash
# Copyright 2025 The ChaosBlade Authors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

# sync_go_mod.sh - Sync branch configurations from Makefile to go.mod
# This script automatically updates dependency versions in go.mod based on branch configurations defined in Makefile

set -e

# Color definitions
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Logging functions
log_info() {
    echo -e "${BLUE}[INFO]${NC} $1"
}

log_success() {
    echo -e "${GREEN}[SUCCESS]${NC} $1"
}

log_warning() {
    echo -e "${YELLOW}[WARNING]${NC} $1"
}

log_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

# Check required tools
check_dependencies() {
    log_info "Checking required tools..."
    
    if ! command -v git &> /dev/null; then
        log_error "git is not installed or not in PATH"
        exit 1
    fi
    
    if ! command -v go &> /dev/null; then
        log_error "go is not installed or not in PATH"
        exit 1
    fi
    
    log_success "Dependency tools check completed"
}

# Extract branch configurations from Makefile
extract_branch_config() {
    log_info "Extracting branch configurations from Makefile..."
    
    # Check if Makefile exists
    if [ ! -f "Makefile" ]; then
        log_error "Makefile does not exist"
        exit 1
    fi
    
    # Extract branch configurations for each project
    BLADE_EXEC_OS_BRANCH=$(grep "^BLADE_EXEC_OS_BRANCH=" Makefile | cut -d'=' -f2)
    BLADE_EXEC_MIDDLEWARE_BRANCH=$(grep "^BLADE_EXEC_MIDDLEWARE_BRANCH=" Makefile | cut -d'=' -f2)
    BLADE_EXEC_CLOUD_BRANCH=$(grep "^BLADE_EXEC_CLOUD_BRANCH=" Makefile | cut -d'=' -f2)
    BLADE_EXEC_CRI_BRANCH=$(grep "^BLADE_EXEC_CRI_BRANCH=" Makefile | cut -d'=' -f2)
    BLADE_OPERATOR_BRANCH=$(grep "^BLADE_OPERATOR_BRANCH=" Makefile | cut -d'=' -f2)
    BLADE_EXEC_JVM_BRANCH=$(grep "^BLADE_EXEC_JVM_BRANCH=" Makefile | cut -d'=' -f2)
    BLADE_EXEC_CPLUS_BRANCH=$(grep "^BLADE_EXEC_CPLUS_BRANCH=" Makefile | cut -d'=' -f2)
    BLADE_SPEC_GO_BRANCH=$(grep "^BLADE_SPEC_GO_BRANCH=" Makefile | cut -d'=' -f2)
    
    # Extract version number
    BLADE_VERSION=$(grep "^BLADE_VERSION :=" Makefile | head -1 | cut -d'=' -f2 | tr -d ' ')
    if [ -z "$BLADE_VERSION" ]; then
        BLADE_VERSION=$(grep "^BLADE_VERSION=" Makefile | head -1 | cut -d'=' -f2 | tr -d ' ')
    fi
    
    # If version number is empty, try to get from environment variable
    if [ -z "$BLADE_VERSION" ]; then
        BLADE_VERSION=${BLADE_VERSION:-"1.7.4"}
    fi
    
    log_info "Extracted branch configurations:"
    log_info "  BLADE_VERSION: $BLADE_VERSION"
    log_info "  BLADE_EXEC_OS_BRANCH: $BLADE_EXEC_OS_BRANCH"
    log_info "  BLADE_EXEC_MIDDLEWARE_BRANCH: $BLADE_EXEC_MIDDLEWARE_BRANCH"
    log_info "  BLADE_EXEC_CLOUD_BRANCH: $BLADE_EXEC_CLOUD_BRANCH"
    log_info "  BLADE_EXEC_CRI_BRANCH: $BLADE_EXEC_CRI_BRANCH"
    log_info "  BLADE_OPERATOR_BRANCH: $BLADE_OPERATOR_BRANCH"
    log_info "  BLADE_EXEC_JVM_BRANCH: $BLADE_EXEC_JVM_BRANCH"
    log_info "  BLADE_EXEC_CPLUS_BRANCH: $BLADE_EXEC_CPLUS_BRANCH"
    log_info "  BLADE_SPEC_GO_BRANCH: $BLADE_SPEC_GO_BRANCH"
}

# Generate version number or branch name
generate_version() {
    local branch=$1
    local base_version=$2
    
    # All branches use branch name directly
    echo "$branch"
}

# Update go.mod file
update_go_mod() {
    log_info "Updating go.mod file..."
    
    # Check if go.mod exists
    if [ ! -f "go.mod" ]; then
        log_error "go.mod does not exist"
        exit 1
    fi
    
    # Backup original go.mod
    cp go.mod go.mod.backup
    log_info "Backed up original go.mod as go.mod.backup"
    
    # Generate version numbers for each project
    OS_VERSION=$(generate_version "$BLADE_EXEC_OS_BRANCH" "$BLADE_VERSION")
    MIDDLEWARE_VERSION=$(generate_version "$BLADE_EXEC_MIDDLEWARE_BRANCH" "$BLADE_VERSION")
    CLOUD_VERSION=$(generate_version "$BLADE_EXEC_CLOUD_BRANCH" "$BLADE_VERSION")
    CRI_VERSION=$(generate_version "$BLADE_EXEC_CRI_BRANCH" "$BLADE_VERSION")
    OPERATOR_VERSION=$(generate_version "$BLADE_OPERATOR_BRANCH" "$BLADE_VERSION")
    JVM_VERSION=$(generate_version "$BLADE_EXEC_JVM_BRANCH" "$BLADE_VERSION")
    CPLUS_VERSION=$(generate_version "$BLADE_EXEC_CPLUS_BRANCH" "$BLADE_VERSION")
    SPEC_GO_VERSION=$(generate_version "$BLADE_SPEC_GO_BRANCH" "$BLADE_VERSION")
    
    log_info "Generated version numbers:"
    log_info "  chaosblade-exec-os: $OS_VERSION"
    log_info "  chaosblade-exec-middleware: $MIDDLEWARE_VERSION"
    log_info "  chaosblade-exec-cloud: $CLOUD_VERSION"
    log_info "  chaosblade-exec-cri: $CRI_VERSION"
    log_info "  chaosblade-operator: $OPERATOR_VERSION"
    log_info "  chaosblade-exec-jvm: $JVM_VERSION"
    log_info "  chaosblade-exec-cplus: $CPLUS_VERSION"
    log_info "  chaosblade-spec-go: $SPEC_GO_VERSION"
    
    # Update dependency versions in go.mod
    # Use sed for replacement, compatible with macOS and Linux
    if [[ "$OSTYPE" == "darwin"* ]]; then
        # macOS
        sed -i '' \
            -e "s|github.com/chaosblade-io/chaosblade-exec-os v[^[:space:]]*|github.com/chaosblade-io/chaosblade-exec-os $OS_VERSION|g" \
            -e "s|github.com/chaosblade-io/chaosblade-exec-middleware v[^[:space:]]*|github.com/chaosblade-io/chaosblade-exec-middleware $MIDDLEWARE_VERSION|g" \
            -e "s|github.com/chaosblade-io/chaosblade-exec-cloud v[^[:space:]]*|github.com/chaosblade-io/chaosblade-exec-cloud $CLOUD_VERSION|g" \
            -e "s|github.com/chaosblade-io/chaosblade-exec-cri v[^[:space:]]*|github.com/chaosblade-io/chaosblade-exec-cri $CRI_VERSION|g" \
            -e "s|github.com/chaosblade-io/chaosblade-operator v[^[:space:]]*|github.com/chaosblade-io/chaosblade-operator $OPERATOR_VERSION|g" \
            -e "s|github.com/chaosblade-io/chaosblade-spec-go v[^[:space:]]*|github.com/chaosblade-io/chaosblade-spec-go $SPEC_GO_VERSION|g" \
            go.mod
    else
        # Linux
        sed -i \
            -e "s|github.com/chaosblade-io/chaosblade-exec-os v[^[:space:]]*|github.com/chaosblade-io/chaosblade-exec-os $OS_VERSION|g" \
            -e "s|github.com/chaosblade-io/chaosblade-exec-middleware v[^[:space:]]*|github.com/chaosblade-io/chaosblade-exec-middleware $MIDDLEWARE_VERSION|g" \
            -e "s|github.com/chaosblade-io/chaosblade-exec-cloud v[^[:space:]]*|github.com/chaosblade-io/chaosblade-exec-cloud $CLOUD_VERSION|g" \
            -e "s|github.com/chaosblade-io/chaosblade-exec-cri v[^[:space:]]*|github.com/chaosblade-io/chaosblade-exec-cri $CRI_VERSION|g" \
            -e "s|github.com/chaosblade-io/chaosblade-operator v[^[:space:]]*|github.com/chaosblade-io/chaosblade-operator $OPERATOR_VERSION|g" \
            -e "s|github.com/chaosblade-io/chaosblade-spec-go v[^[:space:]]*|github.com/chaosblade-io/chaosblade-spec-go $SPEC_GO_VERSION|g" \
            go.mod
    fi
    
    log_success "go.mod file update completed"
}

# Verify update results
verify_update() {
    log_info "Verifying update results..."
    
    # Check go.mod syntax
    if go mod tidy; then
        log_success "go.mod syntax validation passed"
    else
        log_error "go.mod syntax validation failed"
        log_warning "Restoring original go.mod file..."
        mv go.mod.backup go.mod
        exit 1
    fi
    
    # Display updated dependency versions
    log_info "Updated dependency versions:"
    grep -E "github.com/chaosblade-io/(chaosblade-exec-|chaosblade-operator|chaosblade-spec-go)" go.mod | while read line; do
        log_info "  $line"
    done
}

# Clean up backup files
cleanup() {
    if [ "$1" = "--keep-backup" ]; then
        log_info "Keeping backup file go.mod.backup"
    else
        if [ -f "go.mod.backup" ]; then
            rm go.mod.backup
            log_info "Cleaned up backup files"
        fi
    fi
}

# Show help information
show_help() {
    echo "Usage: $0 [options]"
    echo ""
    echo "Options:"
    echo "  --keep-backup    Keep backup file go.mod.backup"
    echo "  --help, -h       Show this help information"
    echo ""
    echo "This script will:"
    echo "  1. Extract branch configurations from Makefile"
    echo "  2. Generate version numbers based on branch configurations"
    echo "  3. Update dependency versions in go.mod"
    echo "  4. Verify update results"
    echo ""
    echo "Examples:"
    echo "  $0                # Sync versions and clean backup"
    echo "  $0 --keep-backup  # Sync versions but keep backup"
}

# Main function
main() {
    local keep_backup=false
    
    # Parse command line arguments
    while [[ $# -gt 0 ]]; do
        case $1 in
            --keep-backup)
                keep_backup=true
                shift
                ;;
            --help|-h)
                show_help
                exit 0
                ;;
            *)
                log_error "Unknown parameter: $1"
                show_help
                exit 1
                ;;
        esac
    done
    
    log_info "Starting to sync Makefile branch configurations to go.mod..."
    
    check_dependencies
    extract_branch_config
    update_go_mod
    verify_update
    
    if [ "$keep_backup" = true ]; then
        cleanup --keep-backup
    else
        cleanup
    fi
    
    log_success "Version sync completed!"
}

# Execute main function
main "$@"
