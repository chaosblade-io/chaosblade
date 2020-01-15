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
	"encoding/json"
	"fmt"
	"os"
	"path"
	"time"

	"github.com/chaosblade-io/chaosblade-spec-go/channel"
	"github.com/chaosblade-io/chaosblade-spec-go/spec"
	"github.com/chaosblade-io/chaosblade-spec-go/util"
)

const ApplicationName = "chaosblade-exec-cplus.jar"
const RemoveAction = "remove"

var cplusJarPath = path.Join(util.GetLibHome(), "cplus", ApplicationName)
var scriptDefaultPath = path.Join(util.GetLibHome(), "cplus", "script")

// 启动 spring boot application，需要校验程序是否已启动
func Prepare(port, scriptLocation string, waitTime int, javaHome string) *spec.Response {
	if scriptLocation == "" {
		scriptLocation = scriptDefaultPath + "/"
	}
	response := preCheck(port, scriptLocation)
	if !response.Success {
		return response
	}
	javaBin, err := getJavaBin(javaHome)
	if err != nil {
		return spec.ReturnFail(spec.Code[spec.FileNotFound], err.Error())
	}
	response = startProxy(port, scriptLocation, javaBin)
	if !response.Success {
		return response
	}
	// wait seconds
	time.Sleep(time.Duration(waitTime) * time.Second)
	return postCheck(port)
}

// getJavaBin returns the java bin path
func getJavaBin(javaHome string) (string, error) {
	if javaHome == "" {
		// check java bin
		response := channel.NewLocalChannel().Run(context.Background(), "java", "-version")
		if response.Success {
			return "java", nil
		}
		// get java home
		javaHome = os.Getenv("JAVA_HOME")
		if javaHome == "" {
			return "", fmt.Errorf("JAVA_HOME not found")
		}
	}
	javaBin := path.Join(javaHome, "bin", "java")
	response := channel.NewLocalChannel().Run(context.Background(), javaBin, "-version")
	if !response.Success {
		return "", fmt.Errorf(response.Err)
	}
	return javaBin, nil
}

func preCheck(port, scriptLocation string) *spec.Response {
	// check spring boot application
	if processExists(port) {
		return spec.ReturnFail(spec.Code[spec.DuplicateError], "the server proxy has been started")
	}
	// check chaosblade-exec-cplus.jar file exists or not
	if !util.IsExist(cplusJarPath) {
		return spec.ReturnFail(spec.Code[spec.FileNotFound],
			fmt.Sprintf("the %s proxy jar file not found in %s dir", ApplicationName, util.GetLibHome()))
	}
	// check script file
	if !util.IsExist(scriptLocation) {
		return spec.ReturnFail(spec.Code[spec.FileNotFound],
			fmt.Sprintf("the %s script file dir not found", scriptLocation))
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
	pids, _ := channel.GetPidsByProcessName(ApplicationName, ctx)
	if pids != nil && len(pids) > 0 {
		return true
	}
	return false
}

// startProxy invokes `nohup java -jar chaosblade-exec-cplus-1.0-SNAPSHOT1.jar --server.port=8703 --script.location=xxx &`
func startProxy(port, scriptLocation, javaBin string) *spec.Response {
	args := fmt.Sprintf("%s -jar %s --server.port=%s --script.location=%s >> %s 2>&1 &",
		javaBin,
		cplusJarPath,
		port, scriptLocation,
		util.GetNohupOutput(util.Blade, util.BladeLog))
	return channel.NewLocalChannel().Run(context.Background(), "nohup", args)
}

func postCheck(port string) *spec.Response {
	result, err, _ := util.Curl(getProxyServiceUrl(port, "status"))
	if err != nil {
		return spec.ReturnFail(spec.Code[spec.CplusProxyCmdError], err.Error())
	}
	var resp spec.Response
	json.Unmarshal([]byte(result), &resp)
	return &resp
}

// 停止 spring boot application
func Revoke(port string) *spec.Response {
	// check process
	if !processExists(port) {
		return spec.ReturnSuccess("process not exists")
	}

	// Get http://127.0.0.1:xxx/remove: EOF, doesn't to check the result
	util.Curl(getProxyServiceUrl(port, RemoveAction))

	time.Sleep(2 * time.Second)
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
