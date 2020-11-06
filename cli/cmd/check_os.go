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
	"errors"
	"fmt"
	"strconv"
	"sync"

	"github.com/chaosblade-io/chaosblade-spec-go/channel"
	"github.com/chaosblade-io/chaosblade-spec-go/spec"
	"github.com/chaosblade-io/chaosblade-spec-go/util"
	"github.com/spf13/cobra"
)

var allCmd []string
var BladeBinPath string

const (
	BladeBin  = "blade"
	OsCommand = "create"
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
	BladeBinPath = util.GetProgramPath() + "/" + BladeBin
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
		fmt.Printf("check os failed! err: %s \n", err.Error())
	}

	// 2. one by one exec cmd
	ch := channel.NewLocalChannel()
	for _, cmd := range allCmd {
		response := ch.Run(context.Background(), BladeBinPath, cmd)
		if response.Success {
			fmt.Printf("%s, success! \n", cmd)
			continue
		}

		fmt.Printf("%s, failed! err: %s", cmd, response.Err)
	}

	return nil
}

// build all os cmd
func (doc *DeteckOsCommand) buildAllOsCmd() error {
	models := AllDeteckModels.Models
	for _, model := range models {
		expName := model.ExpName
		for _, action := range model.Actions() {
			actionName := action.Name()
			cmd := fmt.Sprintf("%s %s %s", OsCommand, expName, actionName)

			// build base cmd by required flag
			for _, flag := range action.Flags() {
				if !flag.FlagRequired() {
					continue
				}

				if flag.FlagDefault() == "" {
					return errors.New("less required parameter, model: " + model.ExpName +
						" action: " + actionName + " parameter: " + flag.FlagName())
				}

				cmd += fmt.Sprintf(" --%s %s", flag.FlagName(), flag.FlagDefault())
				allCmd = append(allCmd, cmd)
			}

			// add other flag
			baseCmd := cmd
			for _, flag := range action.Flags() {
				if flag.FlagRequired() {
					continue
				}

				if flag.FlagDefault() == "" {
					continue
				}

				cmd += fmt.Sprintf(" --%s %s", flag.FlagName(), flag.FlagDefault())
				allCmd = append(allCmd, cmd)
				cmd = baseCmd
			}
		}
	}
	return nil
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
		cmdStr := OsCommand + " " + expModel.Target + " " + expModel.ActionName
		for _, flag := range actionCommandSpec.Flags() {
			value, ok := expModel.ActionFlags[flag.FlagName()]
			if !ok || value == "" {
				value = flag.FlagDefault()
			}

			if flag.FlagRequired() && value == "" {
				fmt.Print("check failed! err: less required parameter \n")
				return nil
			}

			if value == "" {
				continue
			}
			cmdStr += fmt.Sprintf(" --%s %s", flag.FlagName(), value)
		}

		// 3. exec cmd
		response := channel.NewLocalChannel().Run(context.Background(), BladeBinPath, cmdStr)
		if response.Success {
			fmt.Print("check success! \n")
		} else {
			fmt.Printf("check failed! err: %s \n", response.Err)
		}

		return nil
	}
}

func (doc *DeteckOsCommand) actionPostRunEFunc(actionCommand *actionCommand) func(cmd *cobra.Command, args []string) error {
	return func(cmd *cobra.Command, args []string) error {
		return nil
	}
}
