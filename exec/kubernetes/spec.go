/*
 * Copyright 1999-2020 Alibaba Group Holding Ltd.
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

//var log = logf.Log.WithName("Kubernetes")

func NewCommandModelSpec() spec.ExpModelCommandSpec {
	return &CommandModelSpec{
		spec.BaseExpModelCommandSpec{
			ExpActions: []spec.ExpActionCommandSpec{},
			ExpFlags: []spec.ExpFlagSpec{
				KubeConfigFlag, WaitingTimeFlag,
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
	return "blade create k8s node-cpu fullload --names cn-hangzhou.192.168.0.205 --cpu-percent 80 --kubeconfig ~/.kube/config"
}
