package main

import (
	"github.com/spf13/cobra"
	"github.com/chaosblade-io/chaosblade/exec"
	"strings"
	"github.com/chaosblade-io/chaosblade/transport"
	"fmt"
	"context"
)

type DestroyCommand struct {
	baseCommand
	exp *expCommand
}

func (dc *DestroyCommand) Init() {
	dc.command = &cobra.Command{
		Use:     "destroy UID",
		Short:   "Destroy a chaos experiment",
		Long:    "Destroy a chaos experiment by experiment uid, you can run status command to list",
		Args:    cobra.MinimumNArgs(1),
		Aliases: []string{"d"},
		Example: destroyExample(),
		RunE: func(cmd *cobra.Command, args []string) error {
			return dc.runDestroy(cmd, args)
		},
	}
}

// runDestroy
func (dc *DestroyCommand) runDestroy(cmd *cobra.Command, args []string) error {
	uid := args[0]
	model, err := GetDS().QueryExperimentModelByUid(uid)
	if err != nil {
		return transport.ReturnFail(transport.Code[transport.DatabaseError], err.Error())
	}
	if model == nil {
		return transport.Return(transport.Code[transport.DataNotFound])
	}
	if model.Status == "Destroyed" {
		result := fmt.Sprintf("command: %s %s %s, destroy time: %s",
			model.Command, model.SubCommand, model.Flag, model.UpdateTime)
		cmd.Println(transport.ReturnSuccess(result).Print())
		return nil
	}
	var firstCommand = model.Command
	var actionCommand, actionTargetCommand string
	subCommands := strings.Split(model.SubCommand, " ")
	subLength := len(subCommands)
	if subLength > 0 {
		if subLength > 1 {
			actionCommand = subCommands[subLength-1]
			actionTargetCommand = subCommands[subLength-2]
		} else {
			actionCommand = subCommands[0]
			actionTargetCommand = firstCommand
		}
	}
	executor := dc.exp.getExecutor(actionTargetCommand, actionCommand)
	if executor == nil {
		return transport.ReturnFail(transport.Code[transport.ServerError],
			fmt.Sprintf("can't find executor for %s, %s", model.Command, model.SubCommand))
	}
	// covert commandModel to expModel
	expModel := convertCommandModel(actionCommand, actionTargetCommand, model.Flag)
	// set destroy flag
	ctx := exec.SetDestroyFlag(context.Background(), uid)

	preExecutor := dc.exp.preExecutors[model.Command]
	if preExecutor != nil {
		preExec := preExecutor.PreExec(actionCommand, actionTargetCommand, expModel.ActionFlags)
		if preExec != nil {
			channel, ctx_, err := preExec(ctx)
			if err != nil {
				return transport.ReturnFail(transport.Code[transport.PreHandleError], err.Error())
			}
			if channel != nil {
				executor.SetChannel(channel)
			}
			ctx = ctx_
		}
	}
	// execute
	response := executor.Exec(uid, ctx, expModel)
	if !response.Success {
		return response
	}
	// return result
	checkError(GetDS().UpdateExperimentModelByUid(uid, "Destroyed", ""))
	result := fmt.Sprintf("command: %s %s %s", model.Command, model.SubCommand, model.Flag)
	cmd.Println(transport.ReturnSuccess(result).Print())
	return nil
}

// convertCommandModel
func convertCommandModel(action, target, rules string) *exec.ExpModel {
	model := &exec.ExpModel{
		Target:      target,
		ActionName:  action,
		ActionFlags: make(map[string]string, 0),
	}
	flags := strings.Split(rules, " ")
	if len(flags) < 2 {
		return model
	}
	for i := 0; i < len(flags); i += 2 {
		// delete --
		key := flags[i]
		if strings.HasPrefix(key, "--") {
			key = key[2:]
		}
		model.ActionFlags[key] = flags[i+1]
	}
	return model
}

func destroyExample() string {
	return `destroy 47cc0744f1bb`
}
