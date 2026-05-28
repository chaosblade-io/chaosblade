// Copyright 2025 The ChaosBlade Authors
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.

module github.com/chaosblade-io/chaosblade

go 1.25

require (
        github.com/chaosblade-io/chaosblade-exec-cloud v1.8.0
        github.com/chaosblade-io/chaosblade-exec-cri v1.8.0
        github.com/chaosblade-io/chaosblade-exec-middleware v1.8.0
        github.com/chaosblade-io/chaosblade-exec-os v1.8.0
        github.com/chaosblade-io/chaosblade-spec-go v1.8.0
        github.com/glebarez/sqlite v1.11.0
        github.com/olekukonko/tablewriter v0.0.5-0.20201029120751-42e21c7531a3
        github.com/shirou/gopsutil v3.21.11+incompatible
        github.com/spf13/cobra v1.9.1
        github.com/spf13/pflag v1.0.6
        golang.org/x/term v0.37.0
        k8s.io/apimachinery v0.34.1
        k8s.io/client-go v0.34.1
        k8s.io/klog/v2 v2.130.1
        sigs.k8s.io/controller-runtime v0.22.4
)

require (
        cyphar.com/go-pathrs v0.2.1 // indirect
        github.com/AdaLogics/go-fuzz-headers v0.0.0-20240806141605-e8a1dd7889d6 // indirect
        github.com/AdamKorcz/go-118-fuzz-build v0.0.0-20250520111509-a70c2aa677fa // indirect
        github.com/Microsoft/go-winio v0.6.2 // indirect
        github.com/Microsoft/hcsshim v0.13.0 // indirect
        github.com/asaskevich/govalidator v0.0.0-20210307081110-f21760c49a8d // indirect
        github.com/cilium/ebpf v0.17.3 // indirect
        github.com/containerd/cgroups v1.1.0 // indirect
        github.com/containerd/cgroups/v3 v3.0.5 // indirect
        github.com/containerd/containerd v1.7.23 // indirect
        github.com/containerd/containerd/api v1.9.0 // indirect
        github.com/containerd/continuity v0.4.5 // indirect
        github.com/containerd/errdefs v1.0.0 // indirect
        github.com/containerd/errdefs/pkg v0.3.0 // indirect
        github.com/containerd/fifo v1.1.0 // indirect
        github.com/containerd/log v0.1.0 // indirect
        github.com/containerd/platforms v0.2.1 // indirect
        github.com/containerd/ttrpc v1.2.7 // indirect
        github.com/containerd/typeurl/v2 v2.2.3 // indirect
        github.com/coreos/go-systemd/v22 v22.5.0 // indirect
        github.com/cyphar/filepath-securejoin v0.6.0 // indirect
        github.com/davecgh/go-spew v1.1.2-0.20180830191138-d8f796af33cc // indirect
        github.com/dimchansky/utfbom v1.1.1 // indirect
        github.com/distribution/reference v0.6.0 // indirect
        github.com/docker/docker v28.5.1+incompatible // indirect
        github.com/docker/go-connections v0.5.0 // indirect
        github.com/docker/go-events v0.0.0-20250808211157-605354379745 // indirect
        github.com/docker/go-units v0.5.0 // indirect
        github.com/dustin/go-humanize v1.0.1 // indirect
        github.com/emicklei/go-restful/v3 v3.12.2 // indirect
        github.com/evanphx/json-patch/v5 v5.9.11 // indirect
        github.com/felixge/httpsnoop v1.0.4 // indirect
        github.com/fxamacker/cbor/v2 v2.9.0 // indirect
        github.com/glebarez/go-sqlite v1.21.2 // indirect
        github.com/go-logr/logr v1.4.3 // indirect
        github.com/go-logr/stdr v1.2.2 // indirect
        github.com/go-ole/go-ole v1.2.6 // indirect
        github.com/go-openapi/jsonpointer v0.21.0 // indirect
        github.com/go-openapi/jsonreference v0.20.2 // indirect
        github.com/go-openapi/swag v0.23.0 // indirect
        github.com/godbus/dbus/v5 v5.1.0 // indirect
        github.com/gogo/protobuf v1.3.2 // indirect
        github.com/golang/groupcache v0.0.0-20241129210726-2c02b8208cf8 // indirect
        github.com/goodhosts/hostsfile v0.1.6 // indirect
        github.com/google/gnostic-models v0.7.0 // indirect
        github.com/google/go-cmp v0.7.0 // indirect
        github.com/google/uuid v1.6.0 // indirect
        github.com/howeyc/gopass v0.0.0-20190910152052-7cb4b85ec19c // indirect
        github.com/inconshreveable/mousetrap v1.1.0 // indirect
        github.com/jinzhu/inflection v1.0.0 // indirect
        github.com/jinzhu/now v1.1.5 // indirect
        github.com/josharian/intern v1.0.0 // indirect
        github.com/json-iterator/go v1.1.12 // indirect
        github.com/klauspost/compress v1.18.0 // indirect
        github.com/magefile/mage v1.15.0 // indirect
        github.com/mailru/easyjson v0.7.7 // indirect
        github.com/mattn/go-isatty v0.0.17 // indirect
        github.com/mattn/go-runewidth v0.0.7 // indirect
        github.com/moby/docker-image-spec v1.3.1 // indirect
        github.com/moby/locker v1.0.1 // indirect
        github.com/moby/sys/atomicwriter v0.1.0 // indirect
        github.com/moby/sys/mountinfo v0.7.2 // indirect
        github.com/moby/sys/sequential v0.6.0 // indirect
        github.com/moby/sys/signal v0.7.1 // indirect
        github.com/moby/sys/user v0.4.0 // indirect
        github.com/moby/sys/userns v0.1.0 // indirect
        github.com/moby/term v0.5.2 // indirect
        github.com/modern-go/concurrent v0.0.0-20180306012644-bacd9c7ef1dd // indirect
        github.com/modern-go/reflect2 v1.0.3-0.20250322232337-35a7c28c31ee // indirect
        github.com/morikuni/aec v1.1.0 // indirect
        github.com/munnerz/goautoneg v0.0.0-20191010083416-a7dc8b61c822 // indirect
        github.com/opencontainers/go-digest v1.0.0 // indirect
        github.com/opencontainers/image-spec v1.1.1 // indirect
        github.com/opencontainers/runtime-spec v1.2.1 // indirect
        github.com/opencontainers/selinux v1.13.0 // indirect
        github.com/pkg/errors v0.9.1 // indirect
        github.com/pmezard/go-difflib v1.0.1-0.20181226105442-5d4384ee4fb2 // indirect
        github.com/remyoudompheng/bigfft v0.0.0-20230129092748-24d4a6f8daec // indirect
        github.com/sirupsen/logrus v1.9.3 // indirect
        github.com/tklauser/go-sysconf v0.3.12 // indirect
        github.com/tklauser/numcpus v0.6.1 // indirect
        github.com/x448/float16 v0.8.4 // indirect
        github.com/yusufpapurcu/wmi v1.2.4 // indirect
        go.opencensus.io v0.24.0 // indirect
        go.opentelemetry.io/auto/sdk v1.2.1 // indirect
        go.opentelemetry.io/contrib/instrumentation/net/http/otelhttp v0.63.0 // indirect
        go.opentelemetry.io/otel v1.38.0 // indirect
        go.opentelemetry.io/otel/metric v1.38.0 // indirect
        go.opentelemetry.io/otel/trace v1.38.0 // indirect
        go.uber.org/automaxprocs v1.6.0 // indirect
        go.yaml.in/yaml/v2 v2.4.2 // indirect
        go.yaml.in/yaml/v3 v3.0.4 // indirect
        golang.org/x/crypto v0.45.0 // indirect
        golang.org/x/net v0.47.0 // indirect
        golang.org/x/oauth2 v0.30.0 // indirect
        golang.org/x/sync v0.18.0 // indirect
        golang.org/x/sys v0.38.0 // indirect
        golang.org/x/text v0.31.0 // indirect
        golang.org/x/time v0.9.0 // indirect
        google.golang.org/genproto v0.0.0-20251014184007-4626949a642f // indirect
        google.golang.org/genproto/googleapis/rpc v0.0.0-20251014184007-4626949a642f // indirect
        google.golang.org/grpc v1.76.0 // indirect
        google.golang.org/protobuf v1.36.10 // indirect
        gopkg.in/inf.v0 v0.9.1 // indirect
        gopkg.in/natefinch/lumberjack.v2 v2.0.0 // indirect
        gopkg.in/yaml.v2 v2.4.0 // indirect
        gopkg.in/yaml.v3 v3.0.1 // indirect
        gorm.io/gorm v1.25.7 // indirect
        gotest.tools/v3 v3.5.2 // indirect
        k8s.io/api v0.34.1 // indirect
        k8s.io/kube-openapi v0.0.0-20250710124328-f3f2b991d03b // indirect
        k8s.io/utils v0.0.0-20250604170112-4c0f3b243397 // indirect
        modernc.org/libc v1.22.5 // indirect
        modernc.org/mathutil v1.5.0 // indirect
        modernc.org/memory v1.5.0 // indirect
        modernc.org/sqlite v1.23.1 // indirect
        sigs.k8s.io/json v0.0.0-20241014173422-cfa47c3a1cc8 // indirect
        sigs.k8s.io/randfill v1.0.0 // indirect
        sigs.k8s.io/structured-merge-diff/v6 v6.3.0 // indirect
        sigs.k8s.io/yaml v1.6.0 // indirect
)