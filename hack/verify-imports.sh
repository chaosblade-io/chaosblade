#!/usr/bin/env bash

set -o errexit
set -o nounset
set -o pipefail

source "$(dirname "$0")/init.sh"
go install golang.org/x/tools/cmd/goimports@latest

diff=$(git_find | xargs goimports -l -local github.com/chaosblade-io/chaosblade 2>&1) || true
if [[ -n "${diff}" ]]; then
  echo "The following files have incorrect import order. Please run ./hack/update-imports.sh to fix them:" >&2
  echo "${diff}" >&2
  exit 1
fi
