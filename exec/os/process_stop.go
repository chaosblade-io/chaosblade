package os

import (
	"context"
	"fmt"
	"path"

	"github.com/chaosblade-io/chaosblade/exec"
	"github.com/chaosblade-io/chaosblade/transport"
)

type StopProcessActionCommandSpec struct {
}

func (*StopProcessActionCommandSpec) Name() string {
	return "fakedeath"
}

func (*StopProcessActionCommandSpec) Aliases() []string {
	return []string{"f"}
}

func (*StopProcessActionCommandSpec) ShortDesc() string {
	return "process fake death"
}

func (*StopProcessActionCommandSpec) LongDesc() string {
	return "process fake death by process id or process name"
}

func (*StopProcessActionCommandSpec) Matchers() []exec.ExpFlagSpec {
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

func (*StopProcessActionCommandSpec) Flags() []exec.ExpFlagSpec {
	return []exec.ExpFlagSpec{}
}

func (*StopProcessActionCommandSpec) Executor(channel exec.Channel) exec.Executor {
	return &StopProcessExecutor{channel}
}

type StopProcessExecutor struct {
	channel exec.Channel
}

func (spe *StopProcessExecutor) Name() string {
	return "fakedeath"
}

var stopProcessBin = "chaos_stopprocess"

func (spe *StopProcessExecutor) Exec(uid string, ctx context.Context, model *exec.ExpModel) *transport.Response {
	if spe.channel == nil {
		return transport.ReturnFail(transport.Code[transport.ServerError], "channel is nil")
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

	if _, ok := exec.IsDestroy(ctx); ok {
		return spe.recoverProcess(flags, ctx)
	} else {
		return spe.stopProcess(flags, ctx)
	}
}

func (spe *StopProcessExecutor) stopProcess(flags string, ctx context.Context) *transport.Response {
	args := "--start"
	flags = fmt.Sprintf("%s %s", args, flags)
	return spe.channel.Run(ctx, path.Join(spe.channel.GetScriptPath(), stopProcessBin), flags)
}

func (spe *StopProcessExecutor) recoverProcess(flags string, ctx context.Context) *transport.Response {
	args := "--stop"
	flags = fmt.Sprintf("%s %s", args, flags)
	return spe.channel.Run(ctx, path.Join(spe.channel.GetScriptPath(), stopProcessBin), flags)
}

func (spe *StopProcessExecutor) SetChannel(channel exec.Channel) {
	spe.channel = channel
}
