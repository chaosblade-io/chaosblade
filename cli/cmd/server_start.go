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
	"net/http"
	"os"
	"path"
	"time"

	"github.com/chaosblade-io/chaosblade-spec-go/channel"
	"github.com/chaosblade-io/chaosblade-spec-go/spec"
	"github.com/chaosblade-io/chaosblade-spec-go/util"
	"github.com/spf13/cobra"
)

const startServerKey = "blade server start --nohup"

type StartServerCommand struct {
	baseCommand
	ip    string
	port  string
	nohup bool
}

func (ssc *StartServerCommand) Init() {
	ssc.command = &cobra.Command{
		Use:     "start",
		Short:   "Start server mode, exposes web services",
		Long:    "Start server mode, exposes web services. Under the mode, you can send http request to trigger experiments",
		Aliases: []string{"s"},
		RunE: func(cmd *cobra.Command, args []string) error {
			return ssc.run(cmd, args)
		},
		Example: startServerExample(),
	}
	ssc.command.Flags().StringVarP(&ssc.ip, "ip", "i", "", "service ip address, default value is *")
	ssc.command.Flags().StringVarP(&ssc.port, "port", "p", "9526", "service port")
	ssc.command.Flags().BoolVarP(&ssc.nohup, "nohup", "n", false, "used by internal")
}

func (ssc *StartServerCommand) run(cmd *cobra.Command, args []string) error {
	// check if the process named `blade server --start` exists or not
	pids, err := channel.NewLocalChannel().GetPidsByProcessName(startServerKey, context.TODO())
	if err != nil {
		return spec.ResponseFailWithFlags(spec.OsCmdExecFailed, startServerKey, err)
	}
	if len(pids) > 0 {
		return spec.ResponseFailWithFlags(spec.ChaosbladeServerStarted)
	}
	if ssc.nohup {
		ssc.start0()
	}
	err = ssc.start()
	if err != nil {
		return err
	}
	cmd.Println(fmt.Sprintf("success, listening on %s:%s", ssc.ip, ssc.port))
	return nil
}

// start used nohup command and check the process
func (ssc *StartServerCommand) start() error {
	// use nohup to invoke blade server start command
	cl := channel.NewLocalChannel()
	bladeBin := path.Join(util.GetProgramPath(), "blade")
	args := fmt.Sprintf("%s server start --nohup --port %s", bladeBin, ssc.port)
	if ssc.ip != "" {
		args = fmt.Sprintf("%s --ip %s", args, ssc.ip)
	}
	ctx := context.Background()
	response := cl.Run(ctx, "nohup", fmt.Sprintf("%s > /dev/null 2>&1 &", args))
	if !response.Success {
		return response
	}
	time.Sleep(time.Second)
	// check process
	pids, err := channel.NewLocalChannel().GetPidsByProcessName(startServerKey, context.TODO())
	if err != nil {
		return spec.ResponseFailWithFlags(spec.OsCmdExecFailed, startServerKey, err)
	}
	if len(pids) == 0 {
		// read logs
		logFile, err := util.GetLogFile(util.Blade)
		if err != nil {
			return spec.ResponseFailWithFlags(spec.OsCmdExecFailed, startServerKey,
				"start blade server failed and can't get log file")
		}
		if !util.IsExist(logFile) {
			return spec.ResponseFailWithFlags(spec.OsCmdExecFailed, startServerKey,
				"start blade server failed and log file does not exist")
		}
		response := cl.Run(context.TODO(), "tail", fmt.Sprintf("-1 %s", logFile))
		if !response.Success {
			return spec.ResponseFailWithFlags(spec.OsCmdExecFailed, startServerKey,
				"start blade server failed and can't read log file")
		}
		return spec.ResponseFailWithFlags(spec.OsCmdExecFailed, startServerKey, response.Err)
	}
	log.Infof(ctx, "start blade server success, listen on %s:%s", ssc.ip, ssc.port)
	return nil
}

// start0 starts web service
func (ssc *StartServerCommand) start0() {
	go func() {
		err := http.ListenAndServe(ssc.ip+":"+ssc.port, nil)
		if err != nil {
			log.Errorf(context.Background(), "start blade server error, %v", err)
			//log.Error(err, "start blade server error")
			os.Exit(1)
		}
	}()
	Register("/chaosblade")
	util.Hold()
}

func Register(requestPath string) {
	http.HandleFunc(requestPath, func(writer http.ResponseWriter, request *http.Request) {
		fmt.Fprintf(writer, spec.ReturnFail(spec.CommandIllegal, "Server mode is disabled").Print())
	})
}

func startServerExample() string {
	return `blade server start --port 8000`
}
