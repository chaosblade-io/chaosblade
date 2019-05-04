package os

import (
	"github.com/chaosblade-io/chaosblade/exec"
	"context"
	"github.com/chaosblade-io/chaosblade/transport"
	"github.com/chaosblade-io/chaosblade/util"
	"fmt"
)

type ScriptExitActionCommand struct {
}

func (*ScriptExitActionCommand) Name() string {
	return "exit"
}

func (*ScriptExitActionCommand) Aliases() []string {
	return []string{}
}

func (*ScriptExitActionCommand) ShortDesc() string {
	return "Exit script"
}

func (*ScriptExitActionCommand) LongDesc() string {
	return "Exit script with specify message and code"
}

func (*ScriptExitActionCommand) Matchers() []exec.ExpFlagSpec {
	return []exec.ExpFlagSpec{}
}

func (*ScriptExitActionCommand) Flags() []exec.ExpFlagSpec {
	// blade create script exit --function-name offline --exit-message "error" --exit-code 2
	return []exec.ExpFlagSpec{
		&exec.ExpFlag{
			Name:     "exit-code",
			Desc:     "Exit code",
			Required: false,
		},
		&exec.ExpFlag{
			Name:     "exit-message",
			Desc:     "Exit message",
			Required: false,
		},
	}
}

func (*ScriptExitActionCommand) Executor(channel exec.Channel) exec.Executor {
	return &ScriptExitExecutor{channel: channel}
}

type ScriptExitExecutor struct {
	channel exec.Channel
}

func (*ScriptExitExecutor) Name() string {
	return "exit"
}

func (see *ScriptExitExecutor) Exec(uid string, ctx context.Context, model *exec.ExpModel) *transport.Response {
	if see.channel == nil {
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
		return see.stop(ctx, scriptFile)
	} else {
		functionName := model.ActionFlags["function-name"]
		if functionName == "" {
			return transport.ReturnFail(transport.Code[transport.IllegalParameters], "must specify --function-name flag")
		}
		exitMessage := model.ActionFlags["exit-message"]
		exitCode := model.ActionFlags["exit-code"]
		return see.start(ctx, scriptFile, functionName, exitMessage, exitCode)
	}
}

func (see *ScriptExitExecutor) start(ctx context.Context, scriptFile, functionName, exitMessage, exitCode string) *transport.Response {
	var content string
	if exitMessage != "" {
		content = fmt.Sprintf(`echo '%s';`, exitMessage)
	}
	if exitCode == "" {
		exitCode = "1"
	}
	content = fmt.Sprintf("%sexit %s", content, exitCode)
	// backup file
	response := backScript(see.channel, scriptFile)
	if !response.Success {
		return response
	}
	response = insertContentToScriptBy(see.channel, functionName, content, scriptFile)
	if !response.Success {
		see.stop(ctx, scriptFile)
	}
	return response
}

func (see *ScriptExitExecutor) stop(ctx context.Context, scriptFile string) *transport.Response {
	return recoverScript(see.channel, scriptFile)
}

func (see *ScriptExitExecutor) SetChannel(channel exec.Channel) {
	see.channel = channel
}
