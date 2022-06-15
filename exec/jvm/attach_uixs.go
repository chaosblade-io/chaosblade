//go:build !windows
// +build !windows

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
package jvm

import (
	"context"
	"fmt"
	"os"
	osuser "os/user"
	"path"
	"strings"

	"github.com/chaosblade-io/chaosblade-spec-go/spec"
	"github.com/chaosblade-io/chaosblade-spec-go/util"
	"github.com/sirupsen/logrus"
)

func attach(uid, pid, port string, ctx context.Context, javaHome string) (*spec.Response, string) {
	username, err := getUsername(pid)
	if err != nil {
		util.Errorf(uid, util.GetRunFuncName(), spec.ProcessGetUsernameFailed.Sprintf(pid, err))
		return spec.ResponseFailWithFlags(spec.ProcessGetUsernameFailed, pid, err), ""
	}
	javaBin, javaHome := getJavaBinAndJavaHome(javaHome, pid, getJavaCommandLine)
	toolsJar := getToolJar(javaHome)
	logrus.Infof("javaBin: %s, javaHome: %s, toolsJar: %s", javaBin, javaHome, toolsJar)
	token, err := getSandboxToken(ctx)
	if err != nil {
		util.Errorf(uid, util.GetRunFuncName(), spec.SandboxCreateTokenFailed.Sprintf(err))
		return spec.ResponseFailWithFlags(spec.SandboxCreateTokenFailed, err), username
	}
	javaArgs := getAttachJvmOpt(toolsJar, token, port, pid)
	currUser, err := osuser.Current()
	if err != nil {
		logrus.Warnf("get current user info failed, %v", err)
	}
	var command string
	if currUser != nil && (currUser.Username == username) {
		command = fmt.Sprintf("%s %s", javaBin, javaArgs)
	} else {
		if currUser != nil {
			logrus.Infof("current user name is %s, not equal %s, so use sudo command to execute",
				currUser.Username, username)
		}
		command = fmt.Sprintf("sudo -u %s %s %s", username, javaBin, javaArgs)
	}
	// TODO for xiniao, solve the JAVA_TOOL_OPTIONS env
	javaToolOptions := os.Getenv("JAVA_TOOL_OPTIONS")
	if javaToolOptions != "" {
		command = fmt.Sprintf("export JAVA_TOOL_OPTIONS=''&& %s", command)
	}
	response := cl.Run(ctx, "", command)
	if !response.Success {
		return response, username
	}
	osCmd := fmt.Sprintf("grep %s", fmt.Sprintf(`%s %s | grep %s | tail -1 | awk -F ";" '{print $3";"$4}'`,
		token, getSandboxTokenFile(username), DefaultNamespace))
	response = cl.Run(ctx, "", osCmd)
	// if attach successfully, the sandbox-agent.jar will write token to local file
	if !response.Success {
		util.Errorf(uid, util.GetRunFuncName(), spec.OsCmdExecFailed.Sprintf(osCmd, response.Err))
		return spec.ResponseFailWithFlags(spec.OsCmdExecFailed, osCmd, response.Err), username
	}
	return response, username
}

func getAttachJvmOpt(toolsJar string, token string, port string, pid string) string {
	jvmOpts := fmt.Sprintf("-Xms128M -Xmx128M -Xnoclassgc -ea -Xbootclasspath/a:%s", toolsJar)
	sandboxHome := path.Join(util.GetLibHome(), "sandbox")
	sandboxLibPath := path.Join(sandboxHome, "lib")
	sandboxAttachArgs := fmt.Sprintf("home=%s;token=%s;server.ip=%s;server.port=%s;namespace=%s",
		sandboxHome, token, "127.0.0.1", port, DefaultNamespace)
	javaArgs := fmt.Sprintf(`%s -jar %s/sandbox-core.jar %s "%s/sandbox-agent.jar" "%s"`,
		jvmOpts, sandboxLibPath, pid, sandboxLibPath, sandboxAttachArgs)
	return javaArgs
}

func getSandboxToken(ctx context.Context) (string, error) {

	response := cl.Run(ctx, "date", "| head | cksum | sed 's/ //g'")
	if !response.Success {
		return "", fmt.Errorf(response.Err)
	}
	token := strings.TrimSpace(response.Result.(string))
	return token, nil
}
