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
	"strconv"

	"github.com/chaosblade-io/chaosblade-spec-go/util"
	"github.com/spf13/cobra"

	"github.com/chaosblade-io/chaosblade-spec-go/spec"

	"github.com/chaosblade-io/chaosblade/exec/cplus"
)

type PrepareCPlusCommand struct {
	baseCommand
	port int
	ip   string
}

func (pc *PrepareCPlusCommand) Init() {
	pc.command = &cobra.Command{
		Use:   "cplus",
		Short: "Active cplus agent.",
		Long:  "Active cplus agent.",
		RunE: func(cmd *cobra.Command, args []string) error {
			return pc.prepareCPlus()
		},
		Example: pc.prepareExample(),
	}
	pc.command.Flags().IntVarP(&pc.port, "port", "p", 8703, "the server port of cplus proxy")
	pc.command.Flags().StringVarP(&pc.ip, "ip", "i", "", "the server ip")
	pc.command.MarkFlagRequired("port")
}

func (pc *PrepareCPlusCommand) prepareExample() string {
	return `prepare cplus --port 8703`
}

func (pc *PrepareCPlusCommand) prepareCPlus() error {
	portStr := strconv.Itoa(pc.port)
	record, err := GetDS().QueryRunningPreByTypeAndProcess(PrepareCPlusType, portStr, "")
	if err != nil {
		util.Errorf("", util.GetRunFuncName(), fmt.Sprintf(spec.ResponseErr[spec.DbQueryFailed].ErrInfo, "query running by type and process", err.Error()))
		return spec.ResponseFailWaitResult(spec.DbQueryFailed, fmt.Sprintf(spec.ResponseErr[spec.DbQueryFailed].ErrInfo, ""),
			fmt.Sprintf(spec.ResponseErr[spec.DbQueryFailed].ErrInfo, "query running by type and process", err.Error()))
	}
	if record == nil || record.Status != Running {
		record, err = insertPrepareRecord(PrepareCPlusType, portStr, portStr, "")
		if err != nil {
			util.Errorf("", util.GetRunFuncName(), fmt.Sprintf(spec.ResponseErr[spec.DbQueryFailed].ErrInfo, ""))
			return spec.ResponseFailWaitResult(spec.DbQueryFailed, fmt.Sprintf(spec.ResponseErr[spec.DbQueryFailed].Err, ""),
				fmt.Sprintf(spec.ResponseErr[spec.DbQueryFailed].ErrInfo, "insert prepare recode", err.Error()))
		}
	}
	response := cplus.Prepare(record.Uid, portStr, pc.ip)
	return handlePrepareResponse(record.Uid, pc.command, response)
}
