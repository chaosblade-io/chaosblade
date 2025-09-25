#!/usr/bin/env bash

set -o errexit
set -o nounset
set -o pipefail

source "$(dirname "$0")/init.sh"
go install golang.org/x/tools/cmd/goimports@latest

# Serially process each file to avoid concurrent write issues
for f in $(git_find); do
  goimports -w -local github.com/chaosblade-io/chaosblade -srcdir "$(dirname "$f")" "$f"
done

