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
	"encoding/json"
	"errors"
	"fmt"
	"strconv"
	"sync"

	"github.com/chaosblade-io/chaosblade-spec-go/channel"
	"github.com/chaosblade-io/chaosblade-spec-go/spec"
	"github.com/spf13/cobra"
)

var allCmd []string
var AllCmd map[string][]string
var BladeBinPath string

const (
	BladeBin        = "blade"
	OperatorCommand = "operator"
)

type DeteckOsCommand struct {
	command *cobra.Command
	*baseExpCommandService
	sw *sync.WaitGroup
}

func (doc *DeteckOsCommand) CobraCmd() *cobra.Command {
	return doc.command
}

func (doc *DeteckOsCommand) Name() string {
	return ""
}

func (doc *DeteckOsCommand) Init() {
	doc.command = &cobra.Command{
		Use:   "os",
		Short: "Check the environment of os for chaosblade",
		Long:  "Check the environment of os for chaosblade",
		RunE: func(cmd *cobra.Command, args []string) error {
			return doc.deteckOsAll()
		},
		Example: doc.detectExample(),
	}
	//BladeBinPath = path.Join(util.GetProgramPath(), BladeBin)
	// todo : delete
	BladeBinPath = "/Users/caimingxia/chaosblade-0.7.0/blade"
	AllCmd = make(map[string][]string, 0)
	doc.baseExpCommandService = newBaseExpDeteckCommandService(doc)
}

func (doc *DeteckOsCommand) detectExample() string {
	return "check os"
}

// check all os action
func (doc *DeteckOsCommand) deteckOsAll() error {
	// 1. build all cmd
	err := doc.buildAllOsCmd()
	if err != nil {
		fmt.Printf("[failed] check os failed! err: %s \n", err.Error())
	}

	// 2. one by one exec cmd
	for program, cmds := range AllCmd {
		switch program {
		case OperatorCommand:
			doc.execOperatorCmd(cmds)
		default:
			doc.execBladeCmd(cmds)
		}
	}
	return nil
}

func (doc *DeteckOsCommand) execBladeCmd(allCmd []string) {
	ch := channel.NewLocalChannel()
	for _, cmd := range allCmd {
		//2.1 create os cmd
		response := ch.Run(context.Background(), BladeBinPath, cmd)
		if !response.Success {
			fmt.Printf("[failed] %s, failed! create err: %s", cmd, response.Err)
			continue
		}
		var res spec.Response
		err := json.Unmarshal([]byte(response.Result.(string)), &res)
		if err != nil {
			fmt.Printf("[failed] %s, failed! create err: %s", cmd, response.Result)
			continue
		}

		// 2.2 destroy os cmd
		response = ch.Run(context.Background(), BladeBinPath, fmt.Sprintf("destroy %s", res.Result.(string)))
		if !response.Success {
			fmt.Printf("[failed] %s, failed! destroy err: %s \n", err.Error())
			continue
		}
		err = json.Unmarshal([]byte(response.Result.(string)), &res)
		if err != nil || !res.Success {
			fmt.Printf("[failed] %s, failed! destroy err: %s", cmd, response.Result)
			continue
		}
		fmt.Printf("[success] %s, success! \n", cmd)
	}
}

func (doc *DeteckOsCommand) execOperatorCmd(allCmd []string) {
	ch := channel.NewLocalChannel()
	if len(allCmd) == 0 {
		return
	}

	for _, cmd := range allCmd {
		response := ch.Run(context.Background(), "", cmd)
		if !response.Success {
			fmt.Printf("[failed] %s, failed! command not found \n", cmd[len("man "):])
			continue
		}

		fmt.Printf("[success] %s, success! \n", cmd[len("man "):])
	}
}

// build all os cmd

func (doc *DeteckOsCommand) buildAllOsCmd() error {
	models := AllDeteckModels.Models
	for _, model := range models {
		expName := model.ExpName
		for _, action := range model.Actions() {
			programs := action.Programs()
			actionName := action.Name()
			if len(programs) != 1 {
				return errors.New("build all commadn by yaml file failed! action programs wrong, model: " + model.ExpName +
					" action: " + actionName)
			}

			cmd := ""
			switch programs[0] {
			case OperatorCommand:
				cmd = fmt.Sprintf("%s %s", expName, actionName)
			default:
				cmd = fmt.Sprintf("%s %s %s", programs[0], expName, actionName)
			}

			// merge matchers and flags
			flags := doc.mergeMatchesAndFlags(action.Matchers(), action.Flags())

			// build cmd by flags and matchers
			cmdArr, err := doc.buildCmdByMatchersAndFlags(flags, expName, actionName, cmd)
			if err != nil {
				return err
			}
			onePrograms, ok := AllCmd[programs[0]]
			if ok {
				AllCmd[programs[0]] = append(onePrograms, cmdArr...)
			} else {
				AllCmd[programs[0]] = cmdArr

			}
		}
	}
	return nil
}

// merge matchers and flags
func (doc *DeteckOsCommand) mergeMatchesAndFlags(matches, flags []spec.ExpFlagSpec) []spec.ExpFlagSpec {

	mergeResult := make([]spec.ExpFlagSpec, 0)
	for _, flag := range flags {
		mergeResult = append(mergeResult, flag)
	}
	for _, matcher := range matches {
		mergeResult = append(mergeResult, matcher)
	}
	return mergeResult
}

func (doc *DeteckOsCommand) buildCmdByMatchersAndFlags(flags []spec.ExpFlagSpec, expName, actionName, cmd string) ([]string, error) {
	var cmdArr []string
	if len(flags) == 0 {
		cmdArr = append(cmdArr, cmd)
		return cmdArr, nil
	}
	// build base cmd by required flag
	for _, flag := range flags {
		if !flag.FlagRequired() {
			continue
		}

		if flag.FlagDefault() == "" {
			return nil, errors.New("build all commadn by yaml file failed! less required parameter, model: " + expName +
				" action: " + actionName + " parameter: " + flag.FlagName())
		}

		cmd += fmt.Sprintf(" --%s %s", flag.FlagName(), flag.FlagDefault())
		cmdArr = append(cmdArr, cmd)
	}

	// add other flag
	baseCmd := cmd
	for _, flag := range flags {
		if flag.FlagRequired() {
			continue
		}

		if flag.FlagDefault() == "" {
			continue
		}

		cmd += fmt.Sprintf(" --%s %s", flag.FlagName(), flag.FlagDefault())
		cmdArr = append(cmdArr, cmd)
		cmd = baseCmd
	}

	return cmdArr, nil
}

// bind flags
func (doc *DeteckOsCommand) bindFlagsFunction() func(commandFlags map[string]func() string, cmd *cobra.Command, specFlags []spec.ExpFlagSpec) {
	return func(commandFlags map[string]func() string, cmd *cobra.Command, specFlags []spec.ExpFlagSpec) {
		//set action flags
		for _, flag := range specFlags {
			flagName := flag.FlagName()
			flagDesc := flag.FlagDesc()

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

// RunE
func (doc *DeteckOsCommand) actionRunEFunc(target, scope string, actionCommand *actionCommand, actionCommandSpec spec.ExpActionCommandSpec) func(cmd *cobra.Command, args []string) error {
	return func(cmd *cobra.Command, args []string) error {
		// 1. build expModel
		expModel := createExpModel(target, scope, actionCommandSpec.Name(), cmd)

		// 2. build cmd
		programs := actionCommandSpec.Programs()
		if len(programs) != 1 {
			fmt.Print("[failed] check failed! err: action program is wrong! \n")
			return nil
		}

		cmdStr := ""
		// 2.1 merge matchers and flags
		flags := doc.mergeMatchesAndFlags(actionCommandSpec.Matchers(), actionCommandSpec.Flags())
		// 2.2 build cmd by flags
		for _, flag := range flags {
			value, ok := expModel.ActionFlags[flag.FlagName()]
			if !ok || value == "" {
				value = flag.FlagDefault()
			}

			if flag.FlagRequired() && value == "" {
				fmt.Print("[failed] check failed! err: less required parameter! \n")
				return nil
			}

			if value == "" {
				continue
			}
			cmdStr += fmt.Sprintf(" --%s %s", flag.FlagName(), value)
		}

		// 3. exec cmd
		var response *spec.Response
		switch programs[0] {
		case OperatorCommand:
			cmdStr = fmt.Sprintf("%s %s %s", expModel.Target, expModel.ActionName, cmdStr)
			response = channel.NewLocalChannel().Run(context.Background(), "", cmdStr)
			if response.Success {
				fmt.Print("[success] check success! \n")
			} else {
				fmt.Printf("[failed] check failed! %s, command not found \n", cmdStr[len("man "):])
			}
		default:
			cmdStr = fmt.Sprintf("%s %s %s %s", programs[0], expModel.Target, expModel.ActionName, cmdStr)
			response = channel.NewLocalChannel().Run(context.Background(), BladeBinPath, cmdStr)
			if response.Success {
				fmt.Print("[success] check success! \n")
			} else {
				fmt.Printf("[failed] check failed! err: %s \n", response.Err)
			}
		}

		return nil
	}
}

func (doc *DeteckOsCommand) actionPostRunEFunc(actionCommand *actionCommand) func(cmd *cobra.Command, args []string) error {
	return func(cmd *cobra.Command, args []string) error {
		return nil
	}
}
