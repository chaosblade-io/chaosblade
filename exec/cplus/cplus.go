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

package cplus

import (
	"context"
	"fmt"
	"path"
	"strings"
	"time"

	"github.com/chaosblade-io/chaosblade-spec-go/channel"
	"github.com/chaosblade-io/chaosblade-spec-go/spec"
	"github.com/chaosblade-io/chaosblade-spec-go/util"
)

const ApplicationName = "chaosblade-exec-cplus"
const RemoveAction = "remove"

var cplusBinPath = path.Join(util.GetLibHome(), "cplus", ApplicationName)
var scriptDefaultPath = path.Join(util.GetLibHome(), "cplus", "script")

// 启动 spring boot application，需要校验程序是否已启动
func Prepare(port, ip string) *spec.Response {

	response := preCheck(port)
	if !response.Success {
		return response
	}
	response = startProxy(port, ip)
	if !response.Success {
		return response
	}
	return postCheck(port)
}

func preCheck(port string) *spec.Response {
	// check spring boot application
	if processExists(port) {
		return spec.ReturnSuccess("the server proxy has been started")
	}
	// check chaosblade-exec-cplus.jar file exists or not
	if !util.IsExist(cplusBinPath) {
		return spec.ReturnFail(spec.Code[spec.FileNotFound],
			fmt.Sprintf("the %s proxy jar file not found in %s dir", ApplicationName, util.GetLibHome()))
	}
	// check script file
	if !util.IsExist(scriptDefaultPath) {
		return spec.ReturnFail(spec.Code[spec.FileNotFound],
			fmt.Sprintf("the %s script file dir not found", scriptDefaultPath))
	}
	// check the port has been used or not
	portInUse := util.CheckPortInUse(port)
	if portInUse {
		return spec.ReturnFail(spec.Code[spec.IllegalParameters],
			fmt.Sprintf("the %s port is in use", port))
	}
	return spec.ReturnSuccess("success")
}

func processExists(port string) bool {
	ctx := context.WithValue(context.Background(), channel.ProcessKey, port)
	pids, _ := channel.NewLocalChannel().GetPidsByProcessName(ApplicationName, ctx)
	if pids != nil && len(pids) > 0 {
		return true
	}
	return false
}

func startProxy(port, ip string) *spec.Response {
	args := fmt.Sprintf("--port %s", port)
	if ip != "" {
		args = fmt.Sprintf("%s --ip %s", args, ip)
	}
	return channel.NewLocalChannel().Run(context.Background(), cplusBinPath, args)
}

func postCheck(port string) *spec.Response {
	result, err, _ := util.Curl(getProxyServiceUrl(port, "status"))
	if err != nil {
		return spec.ReturnFail(spec.Code[spec.CplusProxyCmdError], err.Error())
	}
	return spec.ReturnSuccess(result)
}

// 停止 spring boot application
func Revoke(port string) *spec.Response {
	// check process
	if !processExists(port) {
		return spec.ReturnSuccess("process not exists")
	}

	// Get http://127.0.0.1:xxx/remove: EOF, doesn't to check the result
	util.Curl(getProxyServiceUrl(port, RemoveAction))

	time.Sleep(time.Second)
	ctx := context.WithValue(context.Background(), channel.ExcludeProcessKey, "blade")
	pids, err := channel.NewLocalChannel().GetPidsByProcessName(ApplicationName, ctx)
	if err != nil {
		return spec.ReturnFail(spec.Code[spec.ServerError], fmt.Sprintf("query process failed, %v", err))
	}
	if len(pids) > 0 {
		response := channel.NewLocalChannel().Run(context.Background(), "kill", fmt.Sprintf("-9 %s", strings.Join(pids, " ")))
		if !response.Success {
			return response
		}
	}
	// revoke failed if the check operation returns success
	response := postCheck(port)
	if response.Success {
		return spec.ReturnFail(spec.Code[spec.CplusProxyCmdError], "the process exists")
	}
	return spec.ReturnSuccess("success")
}

func getProxyServiceUrl(port, action string) string {
	return fmt.Sprintf("http://127.0.0.1:%s/%s",
		port, action)
}
