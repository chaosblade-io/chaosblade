package docker

import (
	"github.com/chaosblade-io/chaosblade/exec"
	"context"
	"fmt"
)

// NetworkPreExec
func (*PreExecutor) NetworkPreExec(channel *Channel, containerId string) func(ctx context.Context) (exec.Channel, context.Context, error) {
	return func(ctx context.Context) (exec.Channel, context.Context, error) {
		ctx = context.WithValue(ctx, ContainerNameKey, newContainerName(containerId, "network"))
		flags := fmt.Sprintf("--rm -t --cap-add NET_ADMIN --net container:%s --label monkeyking-target=network", containerId)
		ctx = context.WithValue(ctx, RunCmdKey, flags)
		return channel, ctx, nil
	}
}
