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
	"github.com/chaosblade-io/chaosblade-spec-go/spec"
	"github.com/chaosblade-io/chaosblade-spec-go/util"
	"github.com/shirou/gopsutil/process"
	"github.com/sirupsen/logrus"
)

// attach sandbox to java process
var cl = channel.NewLocalChannel()

const DefaultNamespace = "default"

func Attach(port string, javaHome string, pid string) (*spec.Response, string) {
	// refresh
	response, username := attach(pid, port, context.TODO(), javaHome)
	if !response.Success {
		return response, username
	}
	time.Sleep(5 * time.Second)
	// active
	response = active(port)
	if !response.Success {
		return response, username
	}
	// check
	return check(port), username
}

// curl -s http://localhost:$2/sandbox/default/module/http/chaosblade/status 2>&1
func check(port string) *spec.Response {
	url := getSandboxUrl(port, "chaosblade/status", "")
	result, err, code := util.Curl(url)
	if code == 200 {
		return spec.ReturnSuccess(result)
	}
	if err != nil {
		return spec.ReturnFail(spec.Code[spec.SandboxInvokeError], err.Error())
	}
	return spec.ReturnFail(spec.Code[spec.SandboxInvokeError],
		fmt.Sprintf("response code is %d, result: %s", code, result))
}

// active chaosblade bin/sandbox.sh -p $pid -P $2 -a chaosblade 2>&1
func active(port string) *spec.Response {
	url := getSandboxUrl(port, "sandbox-module-mgr/active", "&ids=chaosblade")
	result, err, code := util.Curl(url)
	if err != nil {
		return spec.ReturnFail(spec.Code[spec.SandboxInvokeError], err.Error())
	}
	if code != 200 {
		return spec.ReturnFail(spec.Code[spec.SandboxInvokeError],
			fmt.Sprintf("active module response code: %d, result: %s", code, result))
	}
	return spec.ReturnSuccess("success")
}

// attach java agent to application process
func attach(pid, port string, ctx context.Context, javaHome string) (*spec.Response, string) {
	username, err := getUsername(pid)
	if err != nil {
		return spec.ReturnFail(spec.Code[spec.StatusError],
			fmt.Sprintf("get username failed by %s pid, %v", pid, err)), ""
	}
	javaBin, javaHome := getJavaBinAndJavaHome(javaHome, pid, getJavaCommandLine)
	toolsJar := getToolJar(javaHome)
	logrus.Debugf("javaBin: %s, javaHome: %s, toolsJar: %s", javaBin, javaHome, toolsJar)
	token, err := getSandboxToken(ctx)
	if err != nil {
		return spec.ReturnFail(spec.Code[spec.ServerError],
			fmt.Sprintf("create sandbox token failed, %v", err)), username
	}
	javaArgs := getAttachJvmOpts(toolsJar, token, port, pid)
	currUser, err := osuser.Current()
	if err != nil {
		logrus.Warnf("get current user info failed, %v", err)
	}
	var response *spec.Response
	if currUser != nil && (currUser.Username == username) {
		response = cl.Run(ctx, javaBin, javaArgs)
	} else {
		if currUser != nil {
			logrus.Debugf("current user name is %s, not equal %s, so use sudo command to execute",
				currUser.Username, username)
		}
		response = cl.Run(ctx, "sudo", fmt.Sprintf("-u %s %s %s", username, javaBin, javaArgs))
	}
	if !response.Success {
		return response, username
	}
	response = cl.Run(ctx, "grep", fmt.Sprintf(`%s %s | grep %s | tail -1 | awk -F ";" '{print $3";"$4}'`,
		token, getSandboxTokenFile(username), DefaultNamespace))
	// if attach successfully, the sandbox-agent.jar will write token to local file
	if !response.Success {
		return spec.ReturnFail(spec.Code[spec.SandboxInvokeError],
			fmt.Sprintf("attach JVM %s failed, loss response; %s", pid, response.Err)), username
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

func getToolJar(javaHome string) string {
	toolsJar := path.Join(util.GetBinPath(), "tools.jar")
	originalJar := path.Join(javaHome, "lib/tools.jar")
	if util.IsExist(originalJar) {
		toolsJar = originalJar
	} else {
		logrus.Warningf("using chaosblade default tools.jar, %s", toolsJar)
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

func getJavaBinAndJavaHome(javaHome string, pid string,
	getJavaCommandLineFunc func(pid string) (commandSlice []string, err error)) (string, string) {
	javaBin := "java"
	if javaHome != "" {
		javaBin = path.Join(javaHome, "bin/java")
		return javaBin, javaHome
	}
	if javaHome = strings.TrimSpace(os.Getenv("JAVA_HOME")); javaHome != "" {
		javaBin = path.Join(javaHome, "bin/java")
		return javaBin, javaHome
	}
	cmdlineSlice, err := getJavaCommandLineFunc(pid)
	if err != nil {
		logrus.WithField("pid", pid).WithError(err).Warningln("get command slice err")
		return javaBin, javaHome
	}
	if len(cmdlineSlice) == 0 {
		logrus.WithField("pid", pid).Warningln("command line is empty")
		return javaBin, javaHome
	}
	javaBin = strings.TrimSpace(cmdlineSlice[0])
	if strings.HasSuffix(javaBin, "/bin/java") {
		javaHome = javaBin[:len(javaBin)-9]
	}
	return javaBin, javaHome
}

func getJavaCommandLine(pid string) (commandSlice []string, err error) {
	// get commands
	processId, err := strconv.Atoi(pid)
	if err != nil {
		logrus.Warningf("convert string value of pid err, %v", err)
		return nil, err
	}
	processObj, err := process.NewProcess(int32(processId))
	if err != nil {
		logrus.WithField("pid", processId).WithError(err).Warningln("new process by processId err")
		return nil, err
	}
	return processObj.CmdlineSlice()
}

func Detach(port string) *spec.Response {
	return shutdown(port)
}

// CheckPortFromSandboxToken will read last line and curl the port for testing connectivity
func CheckPortFromSandboxToken(username string) (port string, err error) {
	port, err = getPortFromSandboxToken(username)
	if err != nil {
		return port, err
	}
	versionUrl := getSandboxUrl(port, "sandbox-info/version", "")
	_, err, _ = util.Curl(versionUrl)
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
func shutdown(port string) *spec.Response {
	url := getSandboxUrl(port, "sandbox-control/shutdown", "")
	result, err, code := util.Curl(url)
	if err != nil {
		return spec.ReturnFail(spec.Code[spec.SandboxInvokeError], err.Error())
	}
	if code != 200 {
		return spec.ReturnFail(spec.Code[spec.SandboxInvokeError],
			fmt.Sprintf("shutdown module response code: %d, result: %s", code, result))
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
