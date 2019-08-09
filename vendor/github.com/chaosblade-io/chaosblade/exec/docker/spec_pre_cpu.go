package docker

import (
	"fmt"
	"context"
	"strings"
	"github.com/chaosblade-io/chaosblade/exec"
)

// CpuPreExec: add necessary args to context before run action command
// channel: k8s channel or local channel
func (*PreExecutor) CpuPreExec(channel *Channel, containerId string) func(ctx context.Context) (exec.Channel, context.Context, error) {
	return func(ctx context.Context) (exec.Channel, context.Context, error) {
		// get cpu set
		response := channel.GetContainerCpuSet(ctx, containerId)
		if !response.Success {
			return nil, nil, fmt.Errorf(response.Err)
		}
		ctx = context.WithValue(ctx, ContainerNameKey, newContainerName(containerId, "cpu"))
		var cpuset = ""
		if response.Result != nil {
			cpuset = response.Result.(string)
			cpuset = strings.TrimSpace(cpuset)
		}
		startFlags := fmt.Sprintf("-d -t --pid container:%s --ipc container:%s --net container:%s --label monkeyking-target=cpu",
			containerId, containerId, containerId)
		if cpuset != "" {
			startFlags = fmt.Sprintf("%s --cpuset-cpus %s", startFlags, cpuset)
		}
		stopFlags := fmt.Sprintf("-t")
		ctx = context.WithValue(ctx, RunCmdKey, startFlags)
		ctx = context.WithValue(ctx, ExecCmdKey, stopFlags)
		ctx = context.WithValue(ctx, BashFlagsKey, true)
		return channel, ctx, nil
	}
}
