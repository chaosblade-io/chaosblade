package cmd

import (
	"context"
	"fmt"
	"strings"

	"github.com/chaosblade-io/chaosblade-spec-go/spec"
	"github.com/spf13/cobra"
)

type DestroyCommand struct {
	baseCommand
	exp *expCommand
}

func (dc *DestroyCommand) Init() {

	dc.command = &cobra.Command{
		Use:     "destroy UID",
		Short:   "Destroy a chaos experiment",
		Long:    "Destroy a chaos experiment by experiment uid which you can run status command to query",
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
		return spec.ReturnFail(spec.Code[spec.DatabaseError], err.Error())
	}
	if model == nil {
		return spec.Return(spec.Code[spec.DataNotFound])
	}
	if model.Status == "Destroyed" {
		result := fmt.Sprintf("command: %s %s %s, destroy time: %s",
			model.Command, model.SubCommand, model.Flag, model.UpdateTime)
		cmd.Println(spec.ReturnSuccess(result).Print())
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
			actionTargetCommand = ""
		}
	}
	executor := dc.exp.getExecutor(firstCommand, actionTargetCommand, actionCommand)
	if executor == nil {
		return spec.ReturnFail(spec.Code[spec.ServerError],
			fmt.Sprintf("can't find executor for %s, %s", model.Command, model.SubCommand))
	}
	if actionTargetCommand == "" {
		actionTargetCommand = firstCommand
	}
	// covert commandModel to expModel
	expModel := spec.ConvertCommandsToExpModel(actionCommand, actionTargetCommand, model.Flag)
	// set destroy flag
	ctx := spec.SetDestroyFlag(context.Background(), uid)

	// execute
	response := executor.Exec(uid, ctx, expModel)
	if !response.Success {
		return response
	}
	// return result
	checkError(GetDS().UpdateExperimentModelByUid(uid, "Destroyed", ""))
	result := fmt.Sprintf("command: %s %s %s", model.Command, model.SubCommand, model.Flag)
	cmd.Println(spec.ReturnSuccess(result).Print())
	return nil
}

func destroyExample() string {
	return `blade destroy 47cc0744f1bb`
}
