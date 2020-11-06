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
	"github.com/spf13/cobra"
)

var allCmd []string

const (
	BladeBin  = "/Users/caimingxia/chaosblade-0.7.0/blade" //"/opt/chaosblade/blade"
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
		Short: "Deteck the environment of os",
		Long:  "Deteck the environment of os is ok for chaosblade or not",
		RunE: func(cmd *cobra.Command, args []string) error {
			return doc.deteckOsAll()
		},
		Example: doc.detectExample(),
	}

	doc.baseExpCommandService = newBaseExpDeteckCommandService(doc)
}

func (doc *DeteckOsCommand) detectExample() string {
	return "deteck os"
}

// deteck all os action
func (doc *DeteckOsCommand) deteckOsAll() error {
	// 1. build all cmd
	err := doc.buildAllOsCmd()
	if err != nil {
		return spec.ReturnFail(spec.Code[spec.IllegalParameters], err.Error())
	}

	// 2. one by one exec cmd
	var result []string
	ch := channel.NewLocalChannel()
	for _, cmd := range allCmd {
		response := ch.Run(context.Background(), BladeBin, cmd)
		if response.Success {
			result = append(result, fmt.Sprintf("%s, success ;", cmd))
			continue
		}

		result = append(result, fmt.Sprintf("%s, failed ! err: %s ;", cmd, response.Err))
	}

	return spec.ReturnSuccess(result)
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
					return errors.New("less required parameter" + flag.FlagName())
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
		//return
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
				return spec.ReturnFail(spec.Code[spec.IllegalParameters], "less required parameter")
			}

			if value == "" {
				continue
			}
			cmdStr += fmt.Sprintf(" --%s %s", flag.FlagName(), value)
		}

		// 3. exec cmd
		return channel.NewLocalChannel().Run(context.Background(), BladeBin, cmdStr)

	}
}

func (doc *DeteckOsCommand) actionPostRunEFunc(actionCommand *actionCommand) func(cmd *cobra.Command, args []string) error {
	return func(cmd *cobra.Command, args []string) error {
		return nil
	}
}
