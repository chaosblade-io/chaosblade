/*
 * Copyright 2025 The ChaosBlade Authors
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 */

package kubernetes

import (
	"github.com/chaosblade-io/chaosblade-spec-go/spec"
)

type CommandModelSpec struct {
	spec.BaseExpModelCommandSpec
}

var KubeConfigFlag = &spec.ExpFlag{
	Name: "kubeconfig",
	Desc: "kubeconfig file",
}

var WaitingTimeFlag = &spec.ExpFlag{
	Name: "waiting-time",
	Desc: "Waiting time for invoking, default value is 20s",
}

var KubectlProxyFlag = &spec.ExpFlag{
	Name: "kubectl-proxy",
	Desc: "Kubectl proxy URL for accessing Kubernetes API, e.g., http://localhost:8001",
}

var TokenFlag = &spec.ExpFlag{
	Name: "token",
	Desc: "Bearer token for Kubernetes API authentication",
}

var KubewizURLFlag = &spec.ExpFlag{
	Name: "kubewiz-url",
	Desc: "Kubewiz core service URL for delegated K8s operations, e.g., http://kubewiz-core:8080",
}

var ClusterUUIDFlag = &spec.ExpFlag{
	Name: "cluster-uuid",
	Desc: "Target cluster UUID in kubewiz (required when using --kubewiz-url)",
}

var KubewizTokenFlag = &spec.ExpFlag{
	Name: "kubewiz-token",
	Desc: "JWT token for kubewiz-core authentication",
}

// var log = logf.Log.WithName("Kubernetes")
func NewCommandModelSpec() spec.ExpModelCommandSpec {
	return &CommandModelSpec{
		spec.BaseExpModelCommandSpec{
			ExpActions: []spec.ExpActionCommandSpec{},
			ExpFlags: []spec.ExpFlagSpec{
				KubeConfigFlag, WaitingTimeFlag, KubectlProxyFlag, TokenFlag,
				KubewizURLFlag, ClusterUUIDFlag, KubewizTokenFlag,
			},
		},
	}
}

func (*CommandModelSpec) Name() string {
	return "k8s"
}

func (*CommandModelSpec) ShortDesc() string {
	return "Kubernetes experiment"
}

func (*CommandModelSpec) LongDesc() string {
	return "Kubernetes experiment, for example kill pod"
}

func (*CommandModelSpec) Example() string {
	return `blade create k8s node-cpu fullload --names cn-hangzhou.192.168.0.205 --cpu-percent 80 --kubeconfig ~/.kube/config

# 使用 kubectl proxy 和 token 认证
# 首先启动 kubectl proxy: kubectl proxy --port=8080
blade create k8s node-cpu fullload --names cn-hangzhou.192.168.0.205 --cpu-percent 80 --kubectl-proxy http://localhost:8080 --token your-token-here`
}
