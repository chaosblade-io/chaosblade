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
	"os"
	"path"
	"strconv"
	"strings"
	"sync"

	"github.com/chaosblade-io/chaosblade-spec-go/channel"
	"github.com/chaosblade-io/chaosblade-spec-go/spec"
	"github.com/chaosblade-io/chaosblade-spec-go/util"
	"github.com/olekukonko/tablewriter"
	"github.com/spf13/cobra"
)

var allCmd []string
var BladeBinPath string
var AllCheckExecCmds []CheckExecCmd

const (
	BladeBin        = "blade"
	OperatorCommand = "operator"
)

type CheckOsCommand struct {
	command *cobra.Command
	*baseExpCommandService
	sw *sync.WaitGroup
}
type ExecResult struct {
	cmd    string
	result string
	info   string
}

type CheckExecCmd struct {
	ExpName    string
	ActionName string
	Scope      string
	ExecResult []*ExecResult
}

func (doc *CheckOsCommand) CobraCmd() *cobra.Command {
	return doc.command
}

func (doc *CheckOsCommand) Name() string {
	return ""
}

func (doc *CheckOsCommand) Init() {
	doc.command = &cobra.Command{
		Use:   "os",
		Short: "Check the environment of os for chaosblade",
		Long:  "Check the environment of os for chaosblade",
		RunE: func(cmd *cobra.Command, args []string) error {
			return doc.checkOsAll()
		},
		Example: doc.detectExample(),
	}
	BladeBinPath = path.Join(util.GetProgramPath(), BladeBin)
	doc.baseExpCommandService = newBaseExpCheckCommandService(doc)
}

func (doc *CheckOsCommand) detectExample() string {
	return "check os"
}

// check all os action
func (doc *CheckOsCommand) checkOsAll() error {
	// 1. build all cmd
	err := doc.buildAllOsCmd()
	if err != nil {
		fmt.Printf("[failed] check os failed! err: %s \n", err.Error())
	}

	// 2. one by one exec cmd
	for _, allCheckExecCmd := range AllCheckExecCmds {
		switch allCheckExecCmd.Scope {
		case OperatorCommand:
			doc.execOperatorCmd(&allCheckExecCmd)
		default:
			doc.execBladeCmd(&allCheckExecCmd, true)
		}
	}

	// 3. output the result
	var output [][]string
	for _, checkExecCmd := range AllCheckExecCmds {
		for _, execResult := range checkExecCmd.ExecResult {
			oneOutput := []string{checkExecCmd.ExpName, checkExecCmd.ActionName, execResult.result, execResult.info}
			output = append(output, oneOutput)
		}
	}
	doc.outPutTheResult(output)
	return nil
}
func (doc *CheckOsCommand) outPutTheResult(output [][]string) {
	fmt.Printf("------------summary----------")
	table := tablewriter.NewWriter(os.Stdout)
	table.SetHeader([]string{"experiment", "command", "result", "info"})
	table.SetRowLine(true)
	table.AppendBulk(output)
	table.SetAutoMergeCellsByColumnIndex([]int{0})
	table.Render()
}

func (doc *CheckOsCommand) execBladeCmd(checkExecCmd *CheckExecCmd, osAll bool) *spec.Response {
	ch := channel.NewLocalChannel()
	var response *spec.Response
	for _, execResult := range checkExecCmd.ExecResult {
		//1.1 create os cmd
		response = ch.Run(context.Background(), BladeBinPath, execResult.cmd)
		var res spec.Response
		if !response.Success {
			execResult.result = "failed"
			execResult.info = fmt.Sprintf("%s, exec failed! create err: %s", execResult.cmd, response.Err)
			response.Err = fmt.Sprintf("[failed] %s, exec failed! create err: %s", execResult.cmd, response.Err)
			if osAll {
				fmt.Printf("[failed] %s, exec failed! create err: %s \n", execResult.cmd, response.Err)
			}
			continue
		}
		err := json.Unmarshal([]byte(response.Result.(string)), &res)
		if err != nil {
			execResult.result = "failed"
			execResult.info = fmt.Sprintf("%s, exec failed! create err: %s", execResult.cmd, response.Result)
			response.Err = fmt.Sprintf("[failed] %s, exec failed! create err: %s", execResult.cmd, response.Result)
			if osAll {
				fmt.Printf("[failed] %s, exec failed! create err: %s \n", execResult.cmd, response.Result)
			}
			continue
		}

		// 1.2 destroy os cmd
		response = ch.Run(context.Background(), BladeBinPath, fmt.Sprintf("destroy %s", res.Result.(string)))
		if !response.Success {
			execResult.result = "failed"
			execResult.info = fmt.Sprintf("%s, exec failed! destroy err: %s", execResult.cmd, response.Err)
			response.Err = fmt.Sprintf("[failed] %s, exec failed! destroy err: %s", execResult.cmd, response.Err)
			if osAll {
				fmt.Printf("[failed] %s, exec failed! destroy err: %s \n", execResult.cmd, response.Err)
			}
			continue
		}

		execResult.result = "success"
		execResult.info = fmt.Sprintf("%s, exec success!", execResult.cmd)
		response.Result = fmt.Sprintf("[success] %s, success!", execResult.cmd)
		if osAll {
			fmt.Printf("[success] %s, success! \n", execResult.cmd)
		}
	}
	return response
}

func (doc *CheckOsCommand) execOperatorCmd(checkExecCmd *CheckExecCmd) {
	ch := channel.NewLocalChannel()
	if len(checkExecCmd.ExecResult) == 0 {
		return
	}

	for _, execResult := range checkExecCmd.ExecResult {
		cmdArr := strings.Split(execResult.cmd, "|")
		operatorCmd := strings.TrimSpace(cmdArr[0])

		if len(cmdArr) != 2 {
			fmt.Printf("[failed] %s, failed! error: yaml faile is wrong \n", execResult.cmd)
			execResult.info = fmt.Sprintf("yaml faile is wrong")
			execResult.result = "failed"
			continue
		}
		if !ch.IsCommandAvailable(context.TODO(), operatorCmd) {
			fmt.Printf("[failed] %s, failed! error: `%s` command not install \n", cmdArr[1], operatorCmd)
			execResult.info = fmt.Sprintf("`%s` command not install", operatorCmd)
			execResult.result = "failed"
			continue
		}

		fmt.Printf("[success] %s, success! `%s` command exists \n", cmdArr[1], operatorCmd)
		execResult.info = fmt.Sprintf("`%s` command exists", operatorCmd)
		execResult.result = "success"
	}
}

// build all os cmd
func (doc *CheckOsCommand) buildAllOsCmd() error {
	models := AllCheckModels.Models
	for _, model := range models {
		expName := model.ExpName
		scope := model.ExpScope
		for _, action := range model.Actions() {
			actionName := action.Name()

			// merge matchers and flags
			flags := doc.mergeMatchesAndFlags(action.Matchers(), action.Flags())

			var execResult []*ExecResult
			var err error
			switch scope {
			case OperatorCommand:
				execResult = doc.buildOperatorCmd(action.Programs(), expName, actionName)
			default:
				execResult, err = doc.buildBladeCmd(action.Programs(), flags, expName, actionName)
				if err != nil {
					return err
				}
			}

			AllCheckExecCmds = append(AllCheckExecCmds, CheckExecCmd{
				ExpName:    expName,
				ActionName: actionName,
				Scope:      scope,
				ExecResult: execResult,
			})
		}
	}
	return nil
}

func (doc *CheckOsCommand) buildOperatorCmd(programs []string, expName, actionName string) []*ExecResult {
	var execResult []*ExecResult
	for _, program := range programs {
		execResult = append(execResult, &ExecResult{
			cmd: fmt.Sprintf("%s|%s %s", program, expName, actionName),
		})
	}
	return execResult
}

func (doc *CheckOsCommand) buildBladeCmd(programs []string, flags []spec.ExpFlagSpec, expName, actionName string) ([]*ExecResult, error) {
	var execResults []*ExecResult
	for _, program := range programs {
		bladeCmd := fmt.Sprintf("%s %s %s", program, expName, actionName)

		// build cmd by flags and matchers
		execResult, err := doc.buildCmdByMatchersAndFlags(flags, expName, actionName, bladeCmd)
		if err != nil {
			return nil, err
		}

		execResults = append(execResults, execResult...)
	}
	return execResults, nil
}

// merge matchers and flags
func (doc *CheckOsCommand) mergeMatchesAndFlags(matches, flags []spec.ExpFlagSpec) []spec.ExpFlagSpec {

	mergeResult := make([]spec.ExpFlagSpec, 0)
	for _, flag := range flags {
		mergeResult = append(mergeResult, flag)
	}
	for _, matcher := range matches {
		mergeResult = append(mergeResult, matcher)
	}
	return mergeResult
}

func (doc *CheckOsCommand) buildCmdByMatchersAndFlags(flags []spec.ExpFlagSpec, expName, actionName, cmd string) ([]*ExecResult, error) {
	var execResult []*ExecResult
	if len(flags) == 0 {
		execResult = append(execResult, &ExecResult{
			cmd: cmd,
		})
		return execResult, nil
	}
	// build base cmd by required flag
	for _, flag := range flags {
		if !flag.FlagRequired() {
			continue
		}

		if flag.FlagDefault() == "" {
			return nil, errors.New("build all command by yaml file failed! less required parameter, model: " + expName +
				" action: " + actionName + " parameter: " + flag.FlagName())
		}

		cmd += fmt.Sprintf(" --%s %s", flag.FlagName(), flag.FlagDefault())
		execResult = append(execResult, &ExecResult{cmd: cmd})
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
		execResult = append(execResult, &ExecResult{cmd: cmd})
		cmd = baseCmd
	}

	return execResult, nil
}

// bind flags
func (doc *CheckOsCommand) bindFlagsFunction() func(commandFlags map[string]func() string, cmd *cobra.Command, specFlags []spec.ExpFlagSpec) {
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
func (doc *CheckOsCommand) actionRunEFunc(target, scope string, actionCommand *actionCommand, actionCommandSpec spec.ExpActionCommandSpec) func(cmd *cobra.Command, args []string) error {
	return func(cmd *cobra.Command, args []string) error {
		// 1. build expModel
		expModel := createExpModel(target, scope, actionCommandSpec.Name(), cmd)
		var response spec.Response

		// 2. build cmd
		programs := actionCommandSpec.Programs()
		// 2.1 merge matchers and flags
		cmdStr := ""
		flags := doc.mergeMatchesAndFlags(actionCommandSpec.Matchers(), actionCommandSpec.Flags())

		// 2.2 build cmd by flags
		for _, flag := range flags {
			// runEFun donot use default
			value, ok := expModel.ActionFlags[flag.FlagName()]
			if flag.FlagRequired() {
				if !ok || value == "" {
					response.Code = spec.ParameterLess.Code
					response.Success = false
					response.Err = fmt.Sprintf("[failed] check failed! err: less required parameter!")
					cmd.Println(response.Print())
					return nil
				}
			}

			if value == "" {
				continue
			}
			cmdStr += fmt.Sprintf(" --%s %s", flag.FlagName(), value)
		}

		// 3. exec cmd
		switch scope {
		case OperatorCommand:
			failedCmd := ""
			successCmd := ""
			checkStr := fmt.Sprintf("%s %s", target, expModel.ActionName)
			for _, program := range programs {
				if channel.NewLocalChannel().IsCommandAvailable(context.TODO(), program) {
					if successCmd == "" {
						successCmd = program
					} else {
						successCmd += "|" + program
					}
				} else {
					if failedCmd == "" {
						failedCmd = program
					} else {
						failedCmd += "|" + program
					}
				}
			}
			if failedCmd != "" {
				response.Code = spec.CommandIllegal.Code
				response.Success = false
				response.Err = fmt.Sprintf("[failed] %s, failed! `%s` command not install", checkStr, failedCmd)
			} else {
				response.Code = spec.OK.Code
				response.Success = true
				response.Result = fmt.Sprintf("[success] %s, success! `%s` command exists", checkStr, successCmd)
			}

		default:
			cmdStr = fmt.Sprintf("%s %s %s %s", programs[0], expModel.Target, expModel.ActionName, cmdStr)
			checkExecCmd := CheckExecCmd{ExpName: target, ActionName: actionCommandSpec.Name(), Scope: scope, ExecResult: []*ExecResult{&ExecResult{cmd: cmdStr}}}

			response = *doc.execBladeCmd(&checkExecCmd, false)
		}
		return &response
	}
}

func (doc *CheckOsCommand) actionPostRunEFunc(actionCommand *actionCommand) func(cmd *cobra.Command, args []string) error {
	return func(cmd *cobra.Command, args []string) error {
		return nil
	}
}
