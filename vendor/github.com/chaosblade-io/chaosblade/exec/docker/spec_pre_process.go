package docker

import (
	"github.com/chaosblade-io/chaosblade/exec"
	"context"
	"fmt"
)

// ProcessPreExec
func (*PreExecutor) ProcessPreExec(channel *Channel, containerId string) func(ctx context.Context) (exec.Channel, context.Context, error) {
	return func(ctx context.Context) (exec.Channel, context.Context, error) {
		if _, ok := exec.IsDestroy(ctx); ok {
			return channel, ctx, nil
		}
		ctx = context.WithValue(ctx, ContainerNameKey, newContainerName(containerId, "process"))
		flags := fmt.Sprintf("--rm -t --pid container:%s --ipc container:%s --net container:%s --label monkeyking-target=process",
			containerId, containerId, containerId)
		ctx = context.WithValue(ctx, RunCmdKey, flags)
		return channel, ctx, nil
	}
}
