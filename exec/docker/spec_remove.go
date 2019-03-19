package docker

import (
	"github.com/chaosblade-io/chaosblade/exec"
	"github.com/chaosblade-io/chaosblade/transport"
	"context"
	"fmt"
)

type removeActionCommand struct {
}

func (*removeActionCommand) Name() string {
	return RemoveAction
}

func (*removeActionCommand) Aliases() []string {
	return []string{RmAction}
}

func (*removeActionCommand) ShortDesc() string {
	return "remove a container"
}

func (*removeActionCommand) LongDesc() string {
	return "remove a container"
}

func (*removeActionCommand) Matchers() []exec.ExpFlagSpec {
	return []exec.ExpFlagSpec{
	}
}

func (*removeActionCommand) Flags() []exec.ExpFlagSpec {
	return []exec.ExpFlagSpec{
		&exec.ExpFlag{
			Name:   ForceFlag,
			Desc:   "force remove",
			NoArgs: true,
		},
	}
}

func (*removeActionCommand) Executor(channel exec.Channel) exec.Executor {
	return &removeActionExecutor{localChannel: exec.NewLocalChannel()}
}

type removeActionExecutor struct {
	localChannel exec.Channel
}

func (*removeActionExecutor) Name() string {
	return "remove"
}

func (e *removeActionExecutor) SetChannel(channel exec.Channel) {
	e.localChannel = channel
}

func (e *removeActionExecutor) Exec(uid string, ctx context.Context, model *exec.ExpModel) *transport.Response {
	containerId := model.ActionFlags[ContainerFlag]
	if containerId == "" {
		return transport.ReturnFail(transport.Code[transport.IllegalParameters], "container id is null")
	}
	if _, ok := exec.IsDestroy(ctx); ok {
		return transport.ReturnSuccess(uid)
	}
	args := fmt.Sprintf("rm %s", containerId)
	forceFlag := model.ActionFlags[ForceFlag]
	if forceFlag != "" {
		args = fmt.Sprintf("rm -f %s", containerId)
	}
	return e.localChannel.Run(ctx, Command, args)
}
