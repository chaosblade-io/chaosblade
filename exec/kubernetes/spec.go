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
	return "k8s node-cpu load --cpu-percent 50 --selector app=demo --coverage—count 1 --kube-config config"
}
