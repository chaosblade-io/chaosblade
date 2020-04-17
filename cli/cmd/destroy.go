/*
 * Copyright 1999-2020 Alibaba Group Holding Ltd.
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 */

package cmd

import (
	"context"
	"fmt"
	"strconv"
	"strings"

	"github.com/chaosblade-io/chaosblade-spec-go/channel"
	"github.com/chaosblade-io/chaosblade-spec-go/spec"
	"github.com/sirupsen/logrus"
	"github.com/spf13/cobra"
)

type DestroyCommand struct {
	baseCommand
	*baseExpCommandService
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
	flags := dc.command.PersistentFlags()
	flags.StringVar(&uid, UidFlag, "", "Set Uid for the experiment, adapt to docker")
	dc.baseExpCommandService = newBaseExpCommandService(dc)
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
	if model.Status == Destroyed {
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
	executor := dc.GetExecutor(firstCommand, actionTargetCommand, actionCommand)
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
	checkError(GetDS().UpdateExperimentModelByUid(uid, Destroyed, ""))
	cmd.Println(spec.ReturnSuccess(expModel).Print())
	return nil
}

func (dc *DestroyCommand) bindFlagsFunction() func(commandFlags map[string]func() string, cmd *cobra.Command, specFlags []spec.ExpFlagSpec) {
	return func(commandFlags map[string]func() string, cmd *cobra.Command, specFlags []spec.ExpFlagSpec) {
		// set action flags
		for _, flag := range specFlags {
			flagName := flag.FlagName()
			flagDesc := flag.FlagDesc()
			if flag.FlagRequiredWhenDestroyed() {
				cmd.MarkPersistentFlagRequired(flagName)
				flagDesc = fmt.Sprintf("%s (required)", flagDesc)
			}
			if flag.FlagNoArgs() {
				var key bool
				cmd.PersistentFlags().BoolVar(&key, flagName, false, flagDesc)
				commandFlags[flagName] = func() string {
					return strconv.FormatBool(key)
				}
			} else {
				var key string
				cmd.PersistentFlags().StringVar(&key, flagName, "", flagDesc)
				commandFlags[flagName] = func() string {
					return key
				}
			}
		}
	}
}

func (dc *DestroyCommand) actionRunEFunc(target, scope string, _ *actionCommand, actionCommandSpec spec.ExpActionCommandSpec) func(cmd *cobra.Command, args []string) error {
	return func(cmd *cobra.Command, args []string) error {
		expModel := createExpModel(target, scope, actionCommandSpec.Name(), cmd)
		// If uid exists, use uid first. If the record cannot be found, then continue to destroy using matchers
		if uid := expModel.ActionFlags["uid"]; uid != "" {
			err := dc.runDestroy(cmd, []string{uid})
			if err == nil {
				return nil
			}
			resp, ok := err.(*spec.Response)
			if ok && resp.Code != spec.Code[spec.DataNotFound].Code {
				return resp
			}
			logrus.Warningf("%s uid not found, so using matchers to continue to destroy", uid)
		}
		// execute experiment
		executor := actionCommandSpec.Executor()
		executor.SetChannel(channel.NewLocalChannel())
		// set destroy flag
		ctx := spec.SetDestroyFlag(context.Background(), spec.UnknownUid)
		// execute
		response := executor.Exec(spec.UnknownUid, ctx, expModel)
		if !response.Success {
			return response
		}

		command := expModel.Target
		subCommand := expModel.ActionName
		if expModel.Scope != "" && expModel.Scope != "host" {
			command = expModel.Scope
			subCommand = fmt.Sprintf("%s %s", expModel.Target, expModel.ActionName)
		}
		// update status by finding related records
		logrus.Infof("destroy by model: %+v, command: %s, subCommand: %s", expModel, command, subCommand)
		experimentModels, err := GetDS().QueryExperimentModelsByCommand(command, subCommand, expModel.ActionFlags)
		if err != nil {
			logrus.Warningf("destroy success but query records failed, %v", err)
		} else {
			for _, record := range experimentModels {
				checkError(GetDS().UpdateExperimentModelByUid(record.Uid, Destroyed, ""))
			}
		}

		cmd.Println(spec.ReturnSuccess(expModel).Print())
		return nil
	}
}

func (dc *DestroyCommand) actionPostRunEFunc(actionCommand *actionCommand) func(cmd *cobra.Command, args []string) error {
	return nil
}

func destroyExample() string {
	return `blade destroy 47cc0744f1bb`
}
