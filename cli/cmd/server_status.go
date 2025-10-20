/*
 * Copyright 2025 The ChaosBlade Authors
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
	"strconv"

	"github.com/shirou/gopsutil/process"
	"github.com/spf13/cobra"

	"github.com/chaosblade-io/chaosblade-spec-go/channel"
	"github.com/chaosblade-io/chaosblade-spec-go/spec"
)

type StatusServerCommand struct {
	baseCommand
}

func (ssc *StatusServerCommand) Init() {
	ssc.command = &cobra.Command{
		Use:     "status",
		Short:   "Prints out the status of blade server",
		Long:    "Prints out the status of blade server",
		Aliases: []string{"s"},
		RunE: func(cmd *cobra.Command, args []string) error {
			return ssc.run(cmd, args)
		},
		Example: statusServerExample(),
	}
}

func (ssc *StatusServerCommand) run(cmd *cobra.Command, args []string) error {
	// check if the process named `blade server --start` exists or not
	pids, err := channel.NewLocalChannel().GetPidsByProcessName(startServerKey, context.TODO())
	if err != nil {
		return spec.ResponseFailWithFlags(spec.OsCmdExecFailed, startServerKey, err)
	}
	if len(pids) != 0 {
		data := map[string]string{
			"status": "up",
			"port":   "",
		}
		pid, err := strconv.Atoi(pids[0])
		if err != nil {
			return spec.ResponseFailWithFlags(spec.ParameterIllegal, "pid", pids[0], err)
		}
		process, err := process.NewProcess(int32(pid))
		if err != nil {
			return spec.ResponseFailWithFlags(spec.ParameterIllegal, "pid", pids[0], err)
		}
		cmdlineSlice, err := process.CmdlineSlice()
		if err != nil {
			return spec.ResponseFailWithFlags(spec.ParameterIllegal, "pid", pids[0], err)
		}
		for idx, cmd := range cmdlineSlice {
			if cmd == "--port" {
				data["port"] = cmdlineSlice[idx+1]
			}
		}
		ssc.command.Println(spec.ReturnSuccess(data).Print())
	} else {
		return spec.ResponseFailWithFlags(spec.ChaosbladeServiceStoped)
	}
	return nil
}

func statusServerExample() string {
	return `blade server status`
}
