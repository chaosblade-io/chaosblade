package docker

import (
	"context"
	"fmt"
	"strings"
	"time"
	"github.com/chaosblade-io/chaosblade/transport"
	"github.com/chaosblade-io/chaosblade/exec"
	"github.com/chaosblade-io/chaosblade/version"
	"path"
)

const (
	RunCmdKey    = "run"
	ExecCmdKey   = "exec"
	BashFlagsKey = "bash"

	ContainerNameKey = "cn"
)

const bladeHome = "/usr/local/chaosblade"
const repository = "registry.cn-hangzhou.aliyuncs.com/chaosblade/chaosblade-agent"

type Channel struct {
	localChannel exec.Channel
	image        string
}

func NewDockerChannel(channel exec.Channel) *Channel {
	return &Channel{
		localChannel: channel,
		image:        fmt.Sprintf("%s:%s", repository, version.Version.Ver),
	}
}

func (c *Channel) Run(ctx context.Context, script, args string) *transport.Response {
	containerNameValue := ctx.Value(ContainerNameKey)
	if containerNameValue == nil {
		return transport.ReturnFail(transport.Code[transport.IllegalParameters], "cannot get container name")
	}
	containerName := containerNameValue.(string)
	if containerName == "" {
		return transport.ReturnFail(transport.Code[transport.IllegalParameters], "less container name")
	}
	if _, ok := exec.IsDestroy(ctx); ok {
		if ctx.Value(ExecCmdKey) != nil {
			flags := ctx.Value(ExecCmdKey).(string)
			response := c.execContainer(ctx, containerName, flags, fmt.Sprintf("%s %s", script, args))
			if response.Success {
				// remove container
				c.rmContainer(ctx, containerName)
			}
			return response
		}
		if ctx.Value(RunCmdKey) != nil {
			flags := ctx.Value(RunCmdKey).(string)
			response := c.runContainer(ctx, containerName, flags, fmt.Sprintf("%s %s", script, args))
			return response
		}
	} else {
		// if start operation, invoke run Command
		if ctx.Value(RunCmdKey) != nil {
			flags := ctx.Value(RunCmdKey).(string)
			if value := ctx.Value(BashFlagsKey); value != nil && value.(bool) {
				// printf does not contain \n, and can not get logs
				args = fmt.Sprintf("%s | xargs echo;bash", args)
			}
			response := c.runContainer(ctx, containerName, flags, fmt.Sprintf("%s %s", script, args))
			if strings.Contains(flags, "-d") {
				time.Sleep(time.Second)
				return c.getContainerLogs(ctx, containerName)
			} else {
				return response
			}
		}
	}
	return transport.ReturnFail(transport.Code[transport.DockerInvokeError], "not support the docker Command")
}

func (c *Channel) GetScriptPath() string {
	return path.Join(bladeHome, "bin")
}

// runContainer
func (c *Channel) runContainer(ctx context.Context, containerName, flags, command string) *transport.Response {
	var args = fmt.Sprintf("run %s --name %s %s sh -c '%s'", flags, containerName, c.image, command)
	return c.localChannel.Run(ctx, Command, args)
}

// execContainer
func (c *Channel) execContainer(ctx context.Context, containerName, flags, command string) *transport.Response {
	var args = fmt.Sprintf("exec %s %s sh -c '%s'", flags, containerName, command)
	return c.localChannel.Run(ctx, Command, args)
}

// rmContainer
func (c *Channel) rmContainer(ctx context.Context, containerName string) *transport.Response {
	var args = fmt.Sprintf("rm -f %s", containerName)
	return c.localChannel.Run(ctx, Command, args)
}

// getContainerLogs
func (c *Channel) getContainerLogs(ctx context.Context, containerName string) *transport.Response {
	var args = fmt.Sprintf("logs %s", containerName)
	return c.localChannel.Run(ctx, Command, args)
}

// GetContainerCpuSet
func (c *Channel) GetContainerCpuSet(ctx context.Context, containerId string) *transport.Response {
	//docker inspect -f {{.HostConfig.CpusetCpus}} e12c90056eed
	var args = fmt.Sprintf("inspect -f {{.HostConfig.CpusetCpus}} %s", containerId)
	return c.localChannel.Run(ctx, Command, args)
}
