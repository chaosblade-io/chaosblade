package kubernetes

import (
	"github.com/chaosblade-io/chaosblade/exec"
	"github.com/chaosblade-io/chaosblade/transport"
	"context"
	"fmt"
	"strconv"
)

type DeleteActionCommandSpec struct {
}

func (*DeleteActionCommandSpec) Name() string {
	return "delete"
}

func (*DeleteActionCommandSpec) Aliases() []string {
	return []string{}
}

func (*DeleteActionCommandSpec) ShortDesc() string {
	return "delete pod or container"
}

func (*DeleteActionCommandSpec) LongDesc() string {
	return "delete pod by pod name or container by container id"
}

func (*DeleteActionCommandSpec) Matchers() []exec.ExpFlagSpec {
	return []exec.ExpFlagSpec{
		&exec.ExpFlag{
			Name: "pod",
			Desc: "pod name",
		},
	}
}

func (*DeleteActionCommandSpec) Flags() []exec.ExpFlagSpec {
	return []exec.ExpFlagSpec{
		&exec.ExpFlag{
			Name:   "force",
			Desc:   "force remove",
			NoArgs: true,
		},
	}
}

func (*DeleteActionCommandSpec) Executor(channels exec.Channel) exec.Executor {
	localChannel := exec.NewLocalChannel()
	return &deleteExecutor{
		localChannel: localChannel,
		k8sChannel:   Channel{localChannel},
	}
}

type deleteExecutor struct {
	localChannel exec.Channel
	k8sChannel   Channel
}

func (e *deleteExecutor) Name() string {
	return "delete"
}

func (e *deleteExecutor) SetChannel(channel exec.Channel) {
	e.k8sChannel.channel = channel
}

func (e *deleteExecutor) Exec(uid string, ctx context.Context, model *exec.ExpModel) *transport.Response {
	// k delete pod <name> <-l> <--all>
	kubeconfig := model.ActionFlags["kubeconfig"]
	namespace := model.ActionFlags["namespace"]
	podName := model.ActionFlags["pod"]
	containerId := model.ActionFlags["container"]
	if containerId != "" {
		return e.deleteContainer(containerId, podName, namespace, kubeconfig)
	}
	if podName != "" {
		force, err := strconv.ParseBool(model.ActionFlags["force"])
		if err != nil {
			force = false
		}
		return e.deletePod(podName, namespace, kubeconfig, force)
	}
	return transport.ReturnFail(transport.Code[transport.IllegalParameters], "less --pod or --container")
}

func (e *deleteExecutor) deletePod(pod, namespace, kubeconfig string, force bool) *transport.Response {
	args := fmt.Sprintf("delete pod %s", pod)
	if namespace != "" {
		args = fmt.Sprintf("%s -n %s", args, namespace)
	}
	if force {
		args = fmt.Sprintf("%s --force", args)
	}
	// execute on localhost
	return e.localChannel.Run(context.Background(), Command, args)
}

func (e *deleteExecutor) deleteContainer(container, pod, namespace, kubeconfig string) *transport.Response {
	bladePod, err := e.k8sChannel.GetBladePodByContainer(container, pod, namespace, kubeconfig)
	if err != nil {
		return transport.ReturnFail(transport.Code[transport.K8sInvokeError], err.Error())
	}
	if bladePod == "" {
		return transport.ReturnFail(transport.Code[transport.IllegalParameters], "container not found")
	}
	cmd := "docker"
	args := fmt.Sprintf("rm -f %s", container)

	ctx := context.WithValue(context.Background(), "podName", bladePod)
	return e.k8sChannel.Run(ctx, cmd, args)
}
