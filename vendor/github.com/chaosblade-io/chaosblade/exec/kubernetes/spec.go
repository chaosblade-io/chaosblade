package kubernetes

import (
	"github.com/chaosblade-io/chaosblade/exec"
	"context"
)

type CommandModelSpec struct {
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
	return "k8s delete --pod <podname> --namespace default"
}

func (*CommandModelSpec) Actions() []exec.ExpActionCommandSpec {
	return []exec.ExpActionCommandSpec{
		&DeleteActionCommandSpec{},
	}
}

func (cms *CommandModelSpec) Flags() []exec.ExpFlagSpec {
	return []exec.ExpFlagSpec{
		&exec.ExpFlag{
			Name: "kubeconfig",
			Desc: "kubeconfig file",
		},
		&exec.ExpFlag{
			Name: "namespace",
			Desc: "namespace",
		},
		&exec.ExpFlag{
			Name: "deployment",
			Desc: "deployment name",
		},
	}
}

type PreExecutor struct {
	k8sChannel *Channel
}

func (cms *CommandModelSpec) PreExecutor() exec.PreExecutor {
	return &PreExecutor{
		k8sChannel: &Channel{
			exec.NewLocalChannel(),
		},
	}
}

// PreExec
func (pe *PreExecutor) PreExec(cmdName, parentCmdName string, flags map[string]string) func(ctx context.Context) (exec.Channel, context.Context, error) {
	// handle k8s redirect action
	switch cmdName {
	case "delete":
		return func(ctx context.Context) (exec.Channel, context.Context, error) {
			return pe.k8sChannel, ctx, nil
		}
	}
	kubeconfig := flags["kubeconfig"]
	namespace := flags["namespace"]
	podName := flags["pod"]
	deployment := flags["deployment"]

	return func(ctx context.Context) (exec.Channel, context.Context, error) {
		ctx = context.WithValue(ctx, "podName", podName)
		ctx = context.WithValue(ctx, "namespace", namespace)
		ctx = context.WithValue(ctx, "kubeconfig", kubeconfig)
		ctx = context.WithValue(ctx, "deployment", deployment)

		return pe.k8sChannel, ctx, nil
	}
}
