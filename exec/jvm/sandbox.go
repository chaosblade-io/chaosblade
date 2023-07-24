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
	"strconv"
	"strings"
	"time"

	"github.com/chaosblade-io/chaosblade-spec-go/channel"
	"github.com/chaosblade-io/chaosblade-spec-go/log"
	"github.com/chaosblade-io/chaosblade-spec-go/spec"
	"github.com/chaosblade-io/chaosblade-spec-go/util"
	"github.com/shirou/gopsutil/process"
)

// attach sandbox to java process
var cl = channel.NewLocalChannel()

const DefaultNamespace = "chaosblade"

func Attach(ctx context.Context, port, javaHome, pid string) (*spec.Response, string) {
	// refresh
	response, username := attach(ctx, pid, port, javaHome)
	if !response.Success {
		return response, username
	}
	time.Sleep(5 * time.Second)
	// active
	response = active(ctx, port)
	if !response.Success {
		return response, username
	}
	// check
	return check(ctx, port), username
}

// curl -s http://localhost:$2/sandbox/default/module/http/chaosblade/status 2>&1
func check(ctx context.Context, port string) *spec.Response {
	url := getSandboxUrl(port, "chaosblade/status", "")
	result, err, code := util.Curl(ctx, url)
	if code == 200 {
		return spec.ReturnSuccess(result)
	}
	if err != nil {
		log.Errorf(ctx, spec.HttpExecFailed.Sprintf(url, err))
		return spec.ResponseFailWithFlags(spec.HttpExecFailed, url, err)
	}
	log.Errorf(ctx, spec.HttpExecFailed.Sprintf(url, result))
	return spec.ResponseFailWithFlags(spec.HttpExecFailed, url, result)
}

// active chaosblade bin/sandbox.sh -p $pid -P $2 -a chaosblade 2>&1
func active(ctx context.Context, port string) *spec.Response {
	url := getSandboxUrl(port, "sandbox-module-mgr/active", "&ids=chaosblade")
	result, err, code := util.Curl(ctx, url)
	if err != nil {
		log.Errorf(ctx, spec.HttpExecFailed.Sprintf(url, err))
		return spec.ResponseFailWithFlags(spec.HttpExecFailed, url, err)
	}
	if code != 200 {
		log.Errorf(ctx, spec.HttpExecFailed.Sprintf(url, result))
		return spec.ResponseFailWithFlags(spec.HttpExecFailed, url, result)
	}
	return spec.ReturnSuccess("success")
}

// attach java agent to application process
func attach(ctx context.Context, pid, port string, javaHome string) (*spec.Response, string) {
	username, err := getUsername(pid)
	if err != nil {
		log.Errorf(ctx, spec.ProcessGetUsernameFailed.Sprintf(pid, err))
		return spec.ResponseFailWithFlags(spec.ProcessGetUsernameFailed, pid, err), ""
	}
	javaBin, javaHome := getJavaBinAndJavaHome(ctx, javaHome, pid, getJavaCommandLine)
	toolsJar := getToolJar(ctx, javaHome)
	log.Infof(ctx, "javaBin: %s, javaHome: %s, toolsJar: %s", javaBin, javaHome, toolsJar)
	token, err := getSandboxToken(ctx)
	if err != nil {
		log.Errorf(ctx, spec.SandboxCreateTokenFailed.Sprintf(err))
		return spec.ResponseFailWithFlags(spec.SandboxCreateTokenFailed, err), username
	}
	javaArgs := getAttachJvmOpts(toolsJar, token, port, pid)
	currUser, err := osuser.Current()
	if err != nil {
		log.Warnf(ctx, "get current user info failed, %v", err)
	}
	var command string
	if currUser != nil && (currUser.Username == username) {
		command = fmt.Sprintf("%s %s", javaBin, javaArgs)
	} else {
		if currUser != nil {
			log.Infof(ctx, "current user name is %s, not equal %s, so use sudo command to execute",
				currUser.Username, username)
		}
		command = fmt.Sprintf("sudo -u %s %s %s", username, javaBin, javaArgs)
	}
	javaToolOptions := os.Getenv("JAVA_TOOL_OPTIONS")
	if javaToolOptions != "" {
		command = fmt.Sprintf("export JAVA_TOOL_OPTIONS='' && %s", command)
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
		log.Errorf(ctx, spec.OsCmdExecFailed.Sprintf(osCmd, response.Err))
		return spec.ResponseFailWithFlags(spec.OsCmdExecFailed, osCmd, response.Err), username
	}
	return response, username
}

func getAttachJvmOpts(toolsJar string, token string, port string, pid string) string {
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
	// create sandbox token
	response := cl.Run(ctx, "date", "| head | cksum | sed 's/ //g'")
	if !response.Success {
		return "", fmt.Errorf(response.Err)
	}
	token := strings.TrimSpace(response.Result.(string))
	return token, nil
}

func getToolJar(ctx context.Context, javaHome string) string {
	toolsJar := path.Join(util.GetLibHome(), "sandbox", "tools.jar")
	originalJar := path.Join(javaHome, "lib/tools.jar")
	if util.IsExist(originalJar) {
		toolsJar = originalJar
	} else {
		log.Warnf(ctx, "using chaosblade default tools.jar, %s", toolsJar)
	}
	return toolsJar
}

func getUsername(pid string) (string, error) {
	p, err := strconv.Atoi(pid)
	if err != nil {
		return "", err
	}
	javaProcess, err := process.NewProcess(int32(p))
	if err != nil {
		return "", err
	}
	return javaProcess.Username()
}

func getJavaBinAndJavaHome(ctx context.Context, javaHome string, pid string,
	getJavaCommandLineFunc func(ctx context.Context, pid string) (commandSlice []string, err error)) (string, string) {
	javaBin := "java"
	if javaHome != "" {
		javaBin = path.Join(javaHome, "bin/java")
		return javaBin, javaHome
	}
	if javaHome = strings.TrimSpace(os.Getenv("JAVA_HOME")); javaHome != "" {
		javaBin = path.Join(javaHome, "bin/java")
		return javaBin, javaHome
	}
	cmdlineSlice, err := getJavaCommandLineFunc(ctx, pid)
	if err != nil {
		log.Warnf(ctx, "get command slice err, pid: %s, err: %v", pid, err)
		return javaBin, javaHome
	}
	if len(cmdlineSlice) == 0 {
		log.Warnf(ctx, "command line is empty, pid: %s", pid)
		return javaBin, javaHome
	}
	javaBin = strings.TrimSpace(cmdlineSlice[0])
	if strings.HasSuffix(javaBin, "/bin/java") {
		javaHome = javaBin[:len(javaBin)-9]
	}
	return javaBin, javaHome
}

func getJavaCommandLine(ctx context.Context, pid string) (commandSlice []string, err error) {
	// get commands
	processId, err := strconv.Atoi(pid)
	if err != nil {
		log.Warnf(ctx, "convert string value of pid err, %v", err)
		return nil, err
	}
	processObj, err := process.NewProcess(int32(processId))
	if err != nil {
		log.Warnf(ctx, "new process by processId err, pid %s, err: %v", pid, err)
		return nil, err
	}
	return processObj.CmdlineSlice()
}

func Detach(ctx context.Context, port string) *spec.Response {
	return shutdown(ctx, port)
}

// CheckPortFromSandboxToken will read last line and curl the port for testing connectivity
func CheckPortFromSandboxToken(ctx context.Context, username string) (port string, err error) {
	port, err = getPortFromSandboxToken(username)
	if err != nil {
		return port, err
	}
	versionUrl := getSandboxUrl(port, "sandbox-info/version", "")
	_, err, _ = util.Curl(ctx, versionUrl)
	if err != nil {
		return "", err
	}
	return port, nil
}

func getPortFromSandboxToken(username string) (port string, err error) {
	response := cl.Run(context.TODO(), "grep",
		fmt.Sprintf(`%s %s | tail -1 | awk -F ";" '{print $4}'`,
			DefaultNamespace, getSandboxTokenFile(username)))
	if !response.Success {
		return "", fmt.Errorf(response.Err)
	}
	if response.Result == nil {
		return "", fmt.Errorf("get empty from sandbox token file")
	}
	port = strings.TrimSpace(response.Result.(string))
	if port == "" {
		return "", fmt.Errorf("read empty from sandbox token file")
	}
	_, err = strconv.Atoi(port)
	if err != nil {
		return "", fmt.Errorf("can not find port from sandbox token file, %v", err)
	}
	return port, nil
}

// sudo -u $user -H bash bin/sandbox.sh -p $pid -S 2>&1
func shutdown(ctx context.Context, port string) *spec.Response {
	url := getSandboxUrl(port, "sandbox-control/shutdown", "")
	result, err, code := util.Curl(ctx, url)
	if err != nil {
		log.Errorf(ctx, spec.HttpExecFailed.Sprintf(url, err))
		return spec.ResponseFailWithFlags(spec.HttpExecFailed, url, err)
	}
	if code != 200 {
		log.Errorf(ctx, spec.HttpExecFailed.Sprintf(url, result))
		return spec.ResponseFailWithFlags(spec.HttpExecFailed, url, result)
	}
	return spec.ReturnSuccess("success")
}

func getSandboxUrl(port, uri, param string) string {
	// "sandbox-module-mgr/reset"
	return fmt.Sprintf("http://127.0.0.1:%s/sandbox/%s/module/http/%s?1=1%s",
		port, DefaultNamespace, uri, param)
}

func getSandboxTokenFile(username string) string {
	userHome := util.GetSpecifyingUserHome(username)
	return path.Join(userHome, ".sandbox.token")
}
