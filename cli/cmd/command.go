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
	"fmt"
	"strings"
	"time"

	"github.com/chaosblade-io/chaosblade-spec-go/util"

	"github.com/chaosblade-io/chaosblade/data"

	"github.com/chaosblade-io/chaosblade-spec-go/spec"
	"github.com/spf13/cobra"
)

// Command is cli command interface
type Command interface {
	// Init command
	Init()

	// CobraCmd
	CobraCmd() *cobra.Command

	// Name
	Name() string
}

// baseCommand
type baseCommand struct {
	command *cobra.Command
}

func (bc *baseCommand) Init() {
}

func (bc *baseCommand) CobraCmd() *cobra.Command {
	return bc.command
}

func (bc *baseCommand) Name() string {
	return bc.command.Name()
}

var ds data.SourceI

// GetDS returns dataSource
func GetDS() data.SourceI {
	if ds == nil {
		ds = data.GetSource()
	}
	return ds
}

// SetDS for test
func SetDS(source data.SourceI) {
	ds = source
}

// recordExpModel
func (bc *baseCommand) recordExpModel(commandPath string, expModel *spec.ExpModel) (commandModel *data.ExperimentModel,
	response *spec.Response) {
	uid := expModel.ActionFlags[UidFlag]
	var err error
	if uid == "" {
		uid, err = bc.generateUid()
		if err != nil {
			return nil, spec.ResponseFailWithFlags(spec.GenerateUidFailed, err)
		}
	}

	flagsInline := spec.ConvertExpMatchersToString(expModel, func() map[string]spec.Empty {
		return make(map[string]spec.Empty)
	})
	time := time.Now().Format(time.RFC3339Nano)
	command, subCommand, err := parseCommandPath(commandPath)
	if err != nil {
		return nil, spec.ResponseFailWithFlags(spec.CommandIllegal, err)
	}
	commandModel = &data.ExperimentModel{
		Uid:        uid,
		Command:    command,
		SubCommand: subCommand,
		Flag:       flagsInline,
		Status:     Created,
		Error:      "",
		CreateTime: time,
		UpdateTime: time,
	}
	err = GetDS().InsertExperimentModel(commandModel)
	if err != nil {
		return nil, spec.ResponseFailWithFlags(spec.DatabaseError, "insert", err)
	}
	return commandModel, spec.ReturnSuccess(uid)
}

func parseCommandPath(commandPath string) (string, string, error) {
	// chaosbd create docker cpu fullload
	cmds := strings.SplitN(commandPath, " ", 4)
	if len(cmds) < 4 {
		return "", "", fmt.Errorf("not illegal command")
	}
	return cmds[2], cmds[3], nil
}

func (bc *baseCommand) generateUid() (string, error) {
	uid, err := util.GenerateUid()
	if err != nil {
		return "", err
	}
	model, err := GetDS().QueryExperimentModelByUid(uid)
	if err != nil {
		return "", err
	}
	if model == nil {
		return uid, nil
	}
	return bc.generateUid()
}

//AddCommand is add child command to the parent command
func (bc *baseCommand) AddCommand(child Command) {
	child.Init()
	childCmd := child.CobraCmd()
	childCmd.PreRun = func(cmd *cobra.Command, args []string) {
		util.InitLog(util.Blade)
	}
	childCmd.SilenceUsage = true
	childCmd.DisableFlagsInUseLine = true
	childCmd.SilenceErrors = true
	bc.CobraCmd().AddCommand(childCmd)
}
