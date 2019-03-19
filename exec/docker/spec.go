package docker

import (
	"github.com/chaosblade-io/chaosblade/exec"
	"context"
	"fmt"
)

const (
	Command       = "docker"
	RemoveAction  = "remove"
	RmAction      = "rm"
	ContainerFlag = "container"
	ForceFlag     = "force"
)

type CommandModelSpec struct {
}

func (cms *CommandModelSpec) Name() string {
	return Command
}

func (cms *CommandModelSpec) ShortDesc() string {
	return `Execute a docker experiment`
}

func (cms *CommandModelSpec) LongDesc() string {
	return `Execute a docker experiment. The local host must be installed docker command.`
}

func (cms *CommandModelSpec) Example() string {
	return `# Create a remove container experiment
chaosbd create docker remove --container 1c8986a4f899

# Create a docker container full cpu load experiment
chaosbd create docker cpu fullload --container 1c8986a4f899`
}

func (cms *CommandModelSpec) Actions() []exec.ExpActionCommandSpec {
	return []exec.ExpActionCommandSpec{
		&removeActionCommand{},
	}
}

func (cms *CommandModelSpec) Flags() []exec.ExpFlagSpec {
	return []exec.ExpFlagSpec{
		&exec.ExpFlag{
			Name:     ContainerFlag,
			Desc:     "container id or name",
			NoArgs:   false,
			Required: true,
		},
	}
}

type PreExecutor struct {
	DockerChannel *Channel
}

func NewPreExecutor(channel exec.Channel) *PreExecutor {
	return &PreExecutor{
		DockerChannel: NewDockerChannel(channel),
	}
}

func (cms *CommandModelSpec) PreExecutor() exec.PreExecutor {
	return NewPreExecutor(exec.NewLocalChannel())
}

func (pe *PreExecutor) PreExec(cmdName, parentCmdName string, flags map[string]string) func(ctx context.Context) (exec.Channel, context.Context, error) {
	// handle docker redirect action
	switch cmdName {
	case RmAction, RemoveAction:
		// 不做处理，有 remove action executor 自行处理
		return func(ctx context.Context) (exec.Channel, context.Context, error) {
			//return pe.DockerChannel, ctx, nil
			return exec.NewLocalChannel(), ctx, nil
		}
	}
	if parentCmdName == "" {
		return func(ctx context.Context) (exec.Channel, context.Context, error) {
			return nil, nil, fmt.Errorf("not support the command %s", cmdName)
		}
	}
	// get container flag value
	containerId := flags[ContainerFlag]
	if containerId == "" {
		return func(ctx context.Context) (exec.Channel, context.Context, error) {
			return nil, nil, fmt.Errorf("container id not be null")
		}
	}
	// handle action parent command type
	switch parentCmdName {
	case "cpu":
		return pe.CpuPreExec(pe.DockerChannel, containerId)
	case "process":
		return pe.ProcessPreExec(pe.DockerChannel, containerId)
	case "network":
		return pe.NetworkPreExec(pe.DockerChannel, containerId)
	default:
		return func(ctx context.Context) (exec.Channel, context.Context, error) {
			return nil, nil, fmt.Errorf("not support the action command for %s", cmdName)
		}
	}
}

func newContainerName(containerId, injectType string) string {
	return fmt.Sprintf("%s-%s", containerId, injectType)
}
