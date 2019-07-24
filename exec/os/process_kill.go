package os

import (
	"context"
	"fmt"
	"path"

	"github.com/chaosblade-io/chaosblade/exec"
	"github.com/chaosblade-io/chaosblade/transport"
)

type KillProcessActionCommandSpec struct {
}

func (*KillProcessActionCommandSpec) Name() string {
	return "kill"
}

func (*KillProcessActionCommandSpec) Aliases() []string {
	return []string{"k"}
}

func (*KillProcessActionCommandSpec) ShortDesc() string {
	return "Kill process"
}

func (*KillProcessActionCommandSpec) LongDesc() string {
	return "Kill process by process id or process name"
}

func (*KillProcessActionCommandSpec) Matchers() []exec.ExpFlagSpec {
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

func (*KillProcessActionCommandSpec) Flags() []exec.ExpFlagSpec {
	return []exec.ExpFlagSpec{}
}

func (*KillProcessActionCommandSpec) Executor(channel exec.Channel) exec.Executor {
	return &KillProcessExecutor{channel}
}

type KillProcessExecutor struct {
	channel exec.Channel
}

func (kpe *KillProcessExecutor) Name() string {
	return "kill"
}

var killProcessBin = "chaos_killprocess"

func (kpe *KillProcessExecutor) Exec(uid string, ctx context.Context, model *exec.ExpModel) *transport.Response {
	if kpe.channel == nil {
		return transport.ReturnFail(transport.Code[transport.ServerError], "channel is nil")
	}
	if _, ok := exec.IsDestroy(ctx); ok {
		return transport.ReturnSuccess(uid)
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
	return kpe.channel.Run(ctx, path.Join(kpe.channel.GetScriptPath(), killProcessBin), flags)
}

func (kpe *KillProcessExecutor) SetChannel(channel exec.Channel) {
	kpe.channel = channel
}
