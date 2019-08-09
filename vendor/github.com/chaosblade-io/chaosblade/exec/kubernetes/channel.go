package kubernetes

import (
	"github.com/chaosblade-io/chaosblade/transport"
	"context"
	"fmt"
	"github.com/chaosblade-io/chaosblade/exec"
	"strings"
	"github.com/chaosblade-io/chaosblade/util"
)

const Command = "kubectl"
const BladeNS = "chaosblade"

type Channel struct {
	channel exec.Channel
}

func (c *Channel) Run(ctx context.Context, script, args string) *transport.Response {
	ns := ctx.Value("namespace")
	namespace := ""
	if ns != nil {
		namespace = ctx.Value("namespace").(string)
	}
	deployment := ""
	dm := ctx.Value("deployment")
	if dm != nil {
		deployment = ctx.Value("deployment").(string)
	}
	if strings.Contains(script, "burncpu.sh") {
		if args == "-e" {
			return c.PatchContainerToPod(deployment, namespace, false)
		} else if args == "-s" {
			return c.PatchContainerToPod(deployment, namespace, true)
		}
	}
	value := ctx.Value("podName")
	if value == nil || value.(string) == "" {
		return transport.ReturnFail(transport.Code[transport.K8sInvokeError], "blade pod must specify")
	}
	arg := fmt.Sprintf(`exec %s %s -- %s %s`, value.(string), namespace, script, args)
	return c.channel.Run(ctx, Command, arg)
}

func (*Channel) GetScriptPath() string {
	return util.GetBinPath()
}

func (c *Channel) PatchContainerToPod(deployment, namespace string, start bool) *transport.Response {
	if deployment == "" {
		return transport.ReturnFail(transport.Code[transport.IllegalParameters], "less --deployment")
	}
	if namespace != "" {
		namespace = fmt.Sprintf("-n %s", namespace)
	}
	var args string
	if start {
		args = fmt.Sprintf(`patch deployment %s %s -p '{"spec":{"template":{"spec":{"containers":[{"name": "chaosblade-tools", "image": "registry.cn-hangzhou.aliyuncs.com/chaosblade/chaosblade-tools:0.0.1"}]}}}}'`, deployment, namespace)
	} else {
		args = fmt.Sprintf(`patch deployment %s %s --type=json -p '[{"op": "remove", "path": "/spec/template/spec/containers/0"}]'`, deployment, namespace)
	}
	response := c.channel.Run(context.Background(), Command, args)
	if response.Success {
		return transport.ReturnSuccess("execute success")
	}
	return response
}

func (c *Channel) GetBladePodByContainer(container, pod, namespace, kubeconfig string) (string, error) {
	ns := "-n default"
	if namespace != "" {
		ns = fmt.Sprintf("-n %s", namespace)
	}
	kc := ""
	if kubeconfig != "" {
		kc = fmt.Sprintf("--kubeconfig %s", kubeconfig)
	}
	args := fmt.Sprintf(`get pods %s %s -o go-template --template="{{range .items}}@{{.metadata.name}}#{{.status.hostIP}}#{{range .status.containerStatuses}}{{.containerID}}#{{end}}{{end}}" | tr '@' '\n' | grep "//%s"`, ns, kc, container)
	response := c.channel.Run(context.Background(), Command, args)
	if !response.Success {
		if strings.TrimSpace(response.Err) == "exit status 1" {
			return "", fmt.Errorf("container not found")
		}
		return "", fmt.Errorf(response.Err)
	}
	if response.Result == "" {
		return "", fmt.Errorf("%s container not found", container)
	}
	datas := strings.Split(response.Result.(string), "#")
	podName := datas[0]
	if pod != "" && strings.TrimSpace(podName) != pod {
		return "", fmt.Errorf("%s container not found in %s pod", container, pod)
	}
	hostIP := datas[1]
	args = fmt.Sprintf("get pod -n %s -o wide |grep %s| awk '{print $1}'", BladeNS, hostIP)
	response = c.channel.Run(context.Background(), Command, args)
	if !response.Success {
		return "", fmt.Errorf(response.Err)
	}
	if response.Result == "" {
		return "", fmt.Errorf("chaosblade pod not found on %s node", hostIP)
	}
	return strings.TrimSpace(response.Result.(string)), nil
}
