package os

import (
	"github.com/chaosblade-io/chaosblade/exec"
	"github.com/chaosblade-io/chaosblade/transport"
	"context"
	"fmt"
	"path"
)

type ProcessCommandModelSpec struct {
}

func (*ProcessCommandModelSpec) Name() string {
	return "process"
}

func (*ProcessCommandModelSpec) ShortDesc() string {
	return "Process experiment"
}

func (*ProcessCommandModelSpec) LongDesc() string {
	return "Process experiment, for example, kill process"
}

func (*ProcessCommandModelSpec) Example() string {
	return "process kill --process tomcat"
}

func (*ProcessCommandModelSpec) Actions() []exec.ExpActionCommandSpec {
	return []exec.ExpActionCommandSpec{
		&KillActionCommandSpec{},
	}
}

func (*ProcessCommandModelSpec) Flags() []exec.ExpFlagSpec {
	return []exec.ExpFlagSpec{}
}

func (*ProcessCommandModelSpec) PreExecutor() exec.PreExecutor {
	return nil
}

type KillActionCommandSpec struct {
}

func (*KillActionCommandSpec) Name() string {
	return "kill"
}

func (*KillActionCommandSpec) Aliases() []string {
	return []string{"k"}
}

func (*KillActionCommandSpec) ShortDesc() string {
	return "Kill process"
}

func (*KillActionCommandSpec) LongDesc() string {
	return "Kill process by process id or process name"
}

func (*KillActionCommandSpec) Matchers() []exec.ExpFlagSpec {
	return []exec.ExpFlagSpec{
		&exec.ExpFlag{
			Name: "process",
			Desc: "Process name",
		},
		&exec.ExpFlag{
			Name: "process-cmd",
			Desc: "Process name in command",
		},
	}
}

func (*KillActionCommandSpec) Flags() []exec.ExpFlagSpec {
	return []exec.ExpFlagSpec{}
}

func (*KillActionCommandSpec) Executor(channel exec.Channel) exec.Executor {
	return &ProcessExecutor{channel}
}

type ProcessExecutor struct {
	channel exec.Channel
}

func (pe *ProcessExecutor) Name() string {
	return "process"
}

var killProcessBin = "chaos_killprocess"

func (pe *ProcessExecutor) Exec(uid string, ctx context.Context, model *exec.ExpModel) *transport.Response {
	if pe.channel == nil {
		return transport.ReturnFail(transport.Code[transport.ServerError], "channel is nil")
	}
	if _, ok := exec.IsDestroy(ctx); ok {
		return transport.ReturnSuccess(uid)
	}
	if model.ActionName != "kill" {
		return transport.ReturnFail(transport.Code[transport.HandlerNotFound],
			fmt.Sprintf("%s action of kill process not found", model.ActionName))
	}
	process := model.ActionFlags["process"]
	processCmd := model.ActionFlags["process-cmd"]
	if process == "" && processCmd == "" {
		return transport.ReturnFail(transport.Code[transport.IllegalParameters], "less process matcher")
	}
	flags := ""
	if process != "" {
		flags = fmt.Sprintf("--process %s", process)
	} else if processCmd != "" {
		flags = fmt.Sprintf("--process-cmd %s", processCmd)
	}
	return pe.channel.Run(ctx, path.Join(pe.channel.GetScriptPath(), killProcessBin), flags)
}

func (pe *ProcessExecutor) SetChannel(channel exec.Channel) {
	pe.channel = channel
}
