#!/usr/bin/env bash

set -o errexit
set -o nounset
set -o pipefail

source "$(dirname "$0")/init.sh"
go install mvdan.cc/gofumpt@latest

# Serially process each file to avoid concurrent write issues
for f in $(git_find); do
  gofumpt -w "$f"
done

