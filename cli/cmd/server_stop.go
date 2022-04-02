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
	"github.com/chaosblade-io/chaosblade-spec-go/log"
	"strings"

	"github.com/chaosblade-io/chaosblade-spec-go/channel"
	"github.com/chaosblade-io/chaosblade-spec-go/spec"
	"github.com/spf13/cobra"
)

type StopServerCommand struct {
	baseCommand
}

func (ssc *StopServerCommand) Init() {
	ssc.command = &cobra.Command{
		Use:   "stop",
		Short: "Stop server mode, closes web services",
		Long:  "Stop server mode, closes web services",
		RunE: func(cmd *cobra.Command, args []string) error {
			return ssc.run(cmd, args)
		},
		Example: closeServerExample(),
	}
}

func (ssc *StopServerCommand) run(cmd *cobra.Command, args []string) error {
	pids, err := channel.NewLocalChannel().GetPidsByProcessName(startServerKey, context.TODO())
	if err != nil {
		return spec.ResponseFailWithFlags(spec.OsCmdExecFailed, startServerKey, err)
	}
	if pids == nil || len(pids) == 0 {
		log.Infof(context.Background(), "the blade server process not found, so return success for stop operation")
		//log.Info("the blade server process not found, so return success for stop operation")
		cmd.Println(spec.ReturnSuccess("success").Print())
		return nil
	}
	response := channel.NewLocalChannel().Run(context.TODO(), "kill", fmt.Sprintf("-9 %s", strings.Join(pids, " ")))
	if !response.Success {
		return response
	}
	response.Result = fmt.Sprintf("pid is %s", strings.Join(pids, " "))
	cmd.Println(response.Print())
	return nil
}

func closeServerExample() string {
	return `blade server stop`
}
