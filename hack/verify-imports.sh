#!/usr/bin/env bash
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
