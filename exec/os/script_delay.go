package os

import (
	"github.com/chaosblade-io/chaosblade/exec"
	"github.com/chaosblade-io/chaosblade/transport"
	"context"
	"github.com/chaosblade-io/chaosblade/util"
	"fmt"
	"strconv"
)

type ScriptDelayActionCommand struct {
}

func (*ScriptDelayActionCommand) Name() string {
	return "delay"
}

func (*ScriptDelayActionCommand) Aliases() []string {
	return []string{}
}

func (*ScriptDelayActionCommand) ShortDesc() string {
	return "Script executed delay"
}

func (*ScriptDelayActionCommand) LongDesc() string {
	return "Sleep in script"
}

func (*ScriptDelayActionCommand) Matchers() []exec.ExpFlagSpec {
	return []exec.ExpFlagSpec{}
}

func (*ScriptDelayActionCommand) Flags() []exec.ExpFlagSpec {
	//blade create script delay --time 2 --function-name start
	return []exec.ExpFlagSpec{
		&exec.ExpFlag{
			Name:     "time",
			Desc:     "sleep time, unit is millisecond",
			Required: true,
		},
	}
}

func (sac *ScriptDelayActionCommand) Executor(channel exec.Channel) exec.Executor {
	return &ScriptDelayExecutor{channel:channel}
}

type ScriptDelayExecutor struct {
	channel exec.Channel
}

func (*ScriptDelayExecutor) Name() string {
	return "delay"
}

func (sde *ScriptDelayExecutor) Exec(uid string, ctx context.Context, model *exec.ExpModel) *transport.Response {
	if sde.channel == nil {
		return transport.ReturnFail(transport.Code[transport.ServerError], "channel is nil")
	}
	scriptFile := model.ActionFlags["file"]
	if scriptFile == "" {
		return transport.ReturnFail(transport.Code[transport.IllegalParameters], "must specify --file flag")
	}
	if !util.IsExist(scriptFile) {
		return transport.ReturnFail(transport.Code[transport.FileNotFound],
			fmt.Sprintf("%s file not found", scriptFile))
	}
	if _, ok := exec.IsDestroy(ctx); ok {
		return sde.stop(ctx, scriptFile)
	} else {
		functionName := model.ActionFlags["function-name"]
		if functionName == "" {
			return transport.ReturnFail(transport.Code[transport.IllegalParameters], "must specify --function-name flag")
		}
		time := model.ActionFlags["time"]
		if time == "" {
			return transport.ReturnFail(transport.Code[transport.IllegalParameters], "must specify --time flag")
		}
		return sde.start(ctx, scriptFile, functionName, time)
	}
}

func (sde *ScriptDelayExecutor) start(ctx context.Context, scriptFile, functionName, time string) *transport.Response {
	t, err := strconv.Atoi(time)
	if err != nil {
		return transport.ReturnFail(transport.Code[transport.IllegalParameters], "time must be positive integer")
	}
	timeInSecond := float32(t) / 1000.0
	// backup file
	response := backScript(sde.channel, scriptFile)
	if !response.Success {
		return response
	}
	response = insertContentToScriptBy(sde.channel, functionName, fmt.Sprintf("sleep %f", timeInSecond), scriptFile)
	if !response.Success {
		sde.stop(ctx, scriptFile)
	}
	return response
}

func (sde *ScriptDelayExecutor) stop(ctx context.Context, scriptFile string) *transport.Response {
	return recoverScript(sde.channel, scriptFile)
}

func (sde *ScriptDelayExecutor) SetChannel(channel exec.Channel) {
	sde.channel = channel
}
