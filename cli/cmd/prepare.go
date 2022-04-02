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
	"github.com/chaosblade-io/chaosblade-spec-go/log"
	"time"

	"github.com/chaosblade-io/chaosblade-spec-go/spec"
	"github.com/chaosblade-io/chaosblade-spec-go/util"
	"github.com/spf13/cobra"

	"github.com/chaosblade-io/chaosblade/data"
)

const (
	PrepareJvmType   = "jvm"
	PrepareK8sType   = "k8s"
	PrepareCPlusType = "cplus"
)

// PrepareCommand defines attach command
type PrepareCommand struct {
	// baseCommand is basic implementation of command interface
	baseCommand
}

// Init attach command operators includes create instance and bind flags
func (pc *PrepareCommand) Init() {
	pc.command = &cobra.Command{
		Use:     "prepare",
		Aliases: []string{"p"},
		Short:   "Prepare to experiment",
		Long:    "Prepare to experiment, for example, attach agent to java process or deploy agent to kubernetes cluster.",
		RunE: func(cmd *cobra.Command, args []string) error {
			return spec.ResponseFailWithFlags(spec.CommandIllegal, "less command type to prepare")
		},
		Example: pc.prepareExample(),
	}
}

func (pc *PrepareCommand) prepareExample() string {
	return `prepare jvm --process tomcat`
}

// insertPrepareRecord
func insertPrepareRecord(prepareType string, processName, port, processId string) (*data.PreparationRecord, error) {
	uid, err := util.GenerateUid()
	if err != nil {
		return nil, err
	}
	record := &data.PreparationRecord{
		Uid:         uid,
		ProgramType: prepareType,
		Process:     processName,
		Port:        port,
		Pid:         processId,
		Status:      Created,
		Error:       "",
		CreateTime:  time.Now().Format(time.RFC3339Nano),
		UpdateTime:  time.Now().Format(time.RFC3339Nano),
	}
	err = GetDS().InsertPreparationRecord(record)
	if err != nil {
		return nil, err
	}
	return record, nil
}

func handlePrepareResponseWithoutExit(ctx context.Context, uid string, cmd *cobra.Command, response *spec.Response) error {
	response.Result = uid
	if !response.Success {
		GetDS().UpdatePreparationRecordByUid(uid, Error, response.Err)
		return response
	}
	err := GetDS().UpdatePreparationRecordByUid(uid, Running, "")
	if err != nil {
		log.Warnf(ctx, "update preparation record error: %s", err.Error())
	}
	return nil
}

func handlePrepareResponse(ctx context.Context, cmd *cobra.Command, response *spec.Response) error {
	uid = ctx.Value(spec.Uid).(string)
	response.Result = uid
	if !response.Success {
		GetDS().UpdatePreparationRecordByUid(uid, Error, response.Err)
		return response
	}
	err := GetDS().UpdatePreparationRecordByUid(uid, Running, "")
	if err != nil {
		log.Warnf(ctx, "update preparation record error: %s", err.Error())
		//log.V(-1).Info("update preparation record error", "err_msg", err.Error())
	}
	response.Result = uid
	cmd.Println(response.Print())
	return nil
}

func updatePreparationPort(uid, port string) error {
	return GetDS().UpdatePreparationPortByUid(uid, port)
}

func updatePreparationPid(uid, pid string) error {
	return GetDS().UpdatePreparationPidByUid(uid, pid)
}
