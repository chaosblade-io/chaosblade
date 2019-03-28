package os

import (
	"context"
	"fmt"
	"github.com/chaosblade-io/chaosblade/exec"
	"github.com/chaosblade-io/chaosblade/transport"
	"path"
)

type CpuCommandModelSpec struct {
}

func (*CpuCommandModelSpec) Name() string {
	return "cpu"
}

func (*CpuCommandModelSpec) ShortDesc() string {
	return "Cpu experiment"
}

func (*CpuCommandModelSpec) LongDesc() string {
	return "Cpu experiment, for example full load"
}

func (*CpuCommandModelSpec) Example() string {
	return "cpu fullload"
}

func (*CpuCommandModelSpec) Actions() []exec.ExpActionCommandSpec {
	return []exec.ExpActionCommandSpec{
		&fullLoadActionCommand{},
	}
}

func (cms *CpuCommandModelSpec) Flags() []exec.ExpFlagSpec {
	return []exec.ExpFlagSpec{
		&exec.ExpFlag{
			Name:     "timeout",
			Desc:     "execute timeout",
			Required: false,
		},
	}
}

func (*CpuCommandModelSpec) PreExecutor() exec.PreExecutor {
	return &cpuPreExecutor{}
}

type cpuPreExecutor struct {
}

func (*cpuPreExecutor) PreExec(cmdName, parentCmdName string, flags map[string]string) func(ctx context.Context) (exec.Channel, context.Context, error) {
	return nil
}

type fullLoadActionCommand struct {
}

func (*fullLoadActionCommand) Name() string {
	return "fullload"
}

func (*fullLoadActionCommand) Aliases() []string {
	return []string{"fl"}
}

func (*fullLoadActionCommand) ShortDesc() string {
	return "cpu fullload"
}

func (*fullLoadActionCommand) LongDesc() string {
	return "cpu fullload"
}

func (*fullLoadActionCommand) Matchers() []exec.ExpFlagSpec {
	return []exec.ExpFlagSpec{}
}

func (*fullLoadActionCommand) Flags() []exec.ExpFlagSpec {
	return []exec.ExpFlagSpec{}
}

func (*fullLoadActionCommand) Executor(channel exec.Channel) exec.Executor {
	return &cpuExecutor{
		channel: channel,
	}
}

type cpuExecutor struct {
	channel exec.Channel
}

func (ce *cpuExecutor) Name() string {
	return "cpu"
}

func (ce *cpuExecutor) SetChannel(channel exec.Channel) {
	ce.channel = channel
}

func (ce *cpuExecutor) Exec(uid string, ctx context.Context, model *exec.ExpModel) *transport.Response {
	timeout := model.ActionFlags["timeout"]
	if timeout == "" {
		timeout = "0"
	}

	if ce.channel == nil {
		return transport.ReturnFail(transport.Code[transport.ServerError], "channel is nil")
	}
	if _, ok := exec.IsDestroy(ctx); ok {
		return ce.stop(ctx)
	} else {
		return ce.start(ctx, timeout)
	}
}

const burnCpuBin = "chaos_burncpu"

func (ce *cpuExecutor) start(ctx context.Context, timeout string) *transport.Response {
	return ce.channel.Run(ctx, path.Join(ce.channel.GetScriptPath(), burnCpuBin),
		fmt.Sprintf("--start --timeout %s", timeout))
}

func (ce *cpuExecutor) stop(ctx context.Context) *transport.Response {
	return ce.channel.Run(ctx, path.Join(ce.channel.GetScriptPath(), burnCpuBin), "--stop")
}
