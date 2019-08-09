package os

import (
	"context"
	"fmt"
	"path"
	"strconv"

	"github.com/chaosblade-io/chaosblade/exec"
	"github.com/chaosblade-io/chaosblade/transport"
)

type MemCommandModelSpec struct {
}

func (*MemCommandModelSpec) Name() string {
	return "mem"
}

func (*MemCommandModelSpec) ShortDesc() string {
	return "Mem experiment"
}

func (*MemCommandModelSpec) LongDesc() string {
	return "Mem experiment, for example load"
}

func (*MemCommandModelSpec) Example() string {
	return "mem load"
}

func (*MemCommandModelSpec) Actions() []exec.ExpActionCommandSpec {
	return []exec.ExpActionCommandSpec{
		&loadActionCommand{},
	}
}

func (cms *MemCommandModelSpec) Flags() []exec.ExpFlagSpec {
	return []exec.ExpFlagSpec{
		&exec.ExpFlag{
			Name:     "mem-percent",
			Desc:     "percent of burn Memory (0-100)",
			Required: false,
		},
	}
}

func (*MemCommandModelSpec) PreExecutor() exec.PreExecutor {
	return &memPreExecutor{}
}

type memPreExecutor struct {
}

func (*memPreExecutor) PreExec(cmdName, parentCmdName string, flags map[string]string) func(ctx context.Context) (exec.Channel, context.Context, error) {
	return nil
}

type loadActionCommand struct {
}

func (*loadActionCommand) Name() string {
	return "load"
}

func (*loadActionCommand) Aliases() []string {
	return []string{}
}

func (*loadActionCommand) ShortDesc() string {
	return "mem load"
}

func (*loadActionCommand) LongDesc() string {
	return "mem load"
}

func (*loadActionCommand) Matchers() []exec.ExpFlagSpec {
	return []exec.ExpFlagSpec{}
}

func (*loadActionCommand) Flags() []exec.ExpFlagSpec {
	return []exec.ExpFlagSpec{}
}

func (*loadActionCommand) Executor(channel exec.Channel) exec.Executor {
	return &memExecutor{
		channel: channel,
	}
}

type memExecutor struct {
	channel exec.Channel
}

func (ce *memExecutor) Name() string {
	return "mem"
}

func (ce *memExecutor) SetChannel(channel exec.Channel) {
	ce.channel = channel
}

func (ce *memExecutor) Exec(uid string, ctx context.Context, model *exec.ExpModel) *transport.Response {
	if ce.channel == nil {
		return transport.ReturnFail(transport.Code[transport.ServerError], "channel is nil")
	}
	if _, ok := exec.IsDestroy(ctx); ok {
		return ce.stop(ctx)
	}
	var memPercent int

	memPercentStr := model.ActionFlags["mem-percent"]
	if memPercentStr != "" {
		var err error
		memPercent, err = strconv.Atoi(memPercentStr)
		if err != nil {
			return transport.ReturnFail(transport.Code[transport.IllegalParameters],
				"--mem-percent value must be a positive integer")
		}
		if memPercent > 100 || memPercent < 0 {
			return transport.ReturnFail(transport.Code[transport.IllegalParameters],
				"--mem-percent value must be a prositive integer and not bigger than 100")
		}
	} else {
		memPercent = 100
	}

	return ce.start(ctx, memPercent)
}

const burnMemBin = "chaos_burnmem"

// start burn mem
func (ce *memExecutor) start(ctx context.Context, memPercent int) *transport.Response {
	args := fmt.Sprintf("--start --mem-percent %d", memPercent)
	return ce.channel.Run(ctx, path.Join(ce.channel.GetScriptPath(), burnMemBin), args)
}

// stop burn mem
func (ce *memExecutor) stop(ctx context.Context) *transport.Response {
	return ce.channel.Run(ctx, path.Join(ce.channel.GetScriptPath(), burnMemBin), "--stop")
}
