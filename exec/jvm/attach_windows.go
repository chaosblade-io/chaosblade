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
	uuid "github.com/satori/go.uuid"
	"golang.org/x/text/encoding/simplifiedchinese"
	"io/ioutil"
	osuser "os/user"
	"path"
	"regexp"
	"syscall"

	"github.com/chaosblade-io/chaosblade-spec-go/spec"
	"github.com/chaosblade-io/chaosblade-spec-go/util"
	"github.com/sirupsen/logrus"
)

// attachWindows java agent to application process
func attach(uid, pid, port string, ctx context.Context, javaHome string) (*spec.Response, string) {
	username, err := getUsername(pid)
	if err != nil {
		util.Errorf(uid, util.GetRunFuncName(), fmt.Sprintf(spec.ResponseErr[spec.ProcessGetUsernameFailed].ErrInfo, pid, err.Error()))
		return spec.ResponseFailWaitResult(spec.ProcessGetUsernameFailed, fmt.Sprintf(spec.ResponseErr[spec.ProcessGetUsernameFailed].Err, uid),
			fmt.Sprintf(spec.ResponseErr[spec.ProcessGetUsernameFailed].ErrInfo, pid, err.Error())), ""
	}
	javaBin, javaHome := getJavaBinAndJavaHome(javaHome, pid, getJavaCommandLine)
	toolsJar := getToolJar(javaHome)
	logrus.Debugf("javaBin: %s, javaHome: %s, toolsJar: %s", javaBin, javaHome, toolsJar)
	token, err := getSandboxToken(ctx)
	if err != nil {
		util.Errorf(uid, util.GetRunFuncName(), fmt.Sprintf(spec.ResponseErr[spec.SandboxCreateTokenFailed].ErrInfo, err.Error()))
		return spec.ResponseFailWaitResult(spec.SandboxCreateTokenFailed, fmt.Sprintf(spec.ResponseErr[spec.SandboxCreateTokenFailed].Err, uid),
			fmt.Sprintf(spec.ResponseErr[spec.SandboxCreateTokenFailed].ErrInfo, err.Error())), username
	}
	javaArgs := getAttachJvmOpts(toolsJar, token, port, pid)
	currUser, err := osuser.Current()
	if err != nil {
		logrus.Warnf("get current user info failed, %v", err)
	}
	var response *spec.Response
	if currUser != nil && (currUser.Username == username) {
		response = cl.RunJava(ctx, javaBin, javaArgs...)
	} else {
		if currUser != nil {
			logrus.Debugf("current user name is %s, not equal %s, so use sudo command to execute",
				currUser.Username, username)
		}

		handle, err := syscall.GetCurrentProcess()
		if err != nil {
			util.Errorf(uid, util.GetRunFuncName(), fmt.Sprintf(spec.ResponseErr[spec.SandboxCreateTokenFailed].ErrInfo, err.Error()))
			return spec.ResponseFailWaitResult(spec.SandboxCreateTokenFailed, fmt.Sprintf(spec.ResponseErr[spec.SandboxCreateTokenFailed].Err, uid),
				fmt.Sprintf(spec.ResponseErr[spec.SandboxCreateTokenFailed].ErrInfo, err.Error())), username
		}
		defer syscall.CloseHandle(handle)

		var token syscall.Token
		err = syscall.OpenProcessToken(handle, syscall.TOKEN_DUPLICATE|syscall.TOKEN_QUERY|syscall.TOKEN_ADJUST_PRIVILEGES|syscall.TOKEN_ASSIGN_PRIMARY, &token)
		if err != nil {
			util.Errorf(uid, util.GetRunFuncName(), fmt.Sprintf(spec.ResponseErr[spec.SandboxCreateTokenFailed].ErrInfo, err.Error()))
			return spec.ResponseFailWaitResult(spec.SandboxCreateTokenFailed, fmt.Sprintf(spec.ResponseErr[spec.SandboxCreateTokenFailed].Err, uid),
				fmt.Sprintf(spec.ResponseErr[spec.SandboxCreateTokenFailed].ErrInfo, err.Error())), username
		}
		defer token.Close()

		ctx = context.WithValue(ctx, "token", token)
		//response = cl.RunWithToken(ctx, javaBin, token, javaArgs...)
		response = cl.RunJava(ctx, javaBin, javaArgs...)
	}
	response.Err = ConvertByte2String([]byte(response.Err), GB18030)
	if !response.Success {
		return response, username
	}

	bytes, err := ioutil.ReadFile(getSandboxTokenFile(username))
	if err != nil {
		response = spec.ReturnFail(spec.Code[spec.DataNotFound], "sandbox file not found")
	} else {
		row := regexp.MustCompile(fmt.Sprintf(`(%s;%s;localhost;)[0-9]+`, DefaultNamespace, token)).FindString(string(bytes))
		response = spec.ReturnSuccess(row)
	}

	// if attach successfully, the sandbox-agent.jar will write token to local file
	if !response.Success {
		util.Errorf(uid, util.GetRunFuncName(), fmt.Sprintf(spec.ResponseErr[spec.FileNotExist].ErrInfo, getSandboxTokenFile(username), response.Err))
		return spec.ResponseFailWaitResult(spec.FileNotExist, fmt.Sprintf(spec.ResponseErr[spec.FileNotExist].Err, uid),
			fmt.Sprintf(spec.ResponseErr[spec.FileNotExist].ErrInfo, getSandboxTokenFile(username), response.Err)), username
	}
	return response, username
}

func getAttachJvmOpts(toolsJar string, token string, port string, pid string) []string {
	var javaArgs []string
	javaArgs = append(javaArgs, "-Xms128M")
	javaArgs = append(javaArgs, "-Xmx128M")
	javaArgs = append(javaArgs, "-Xnoclassgc")
	javaArgs = append(javaArgs, "-ea")
	javaArgs = append(javaArgs, fmt.Sprintf("-Xbootclasspath/a:%s", toolsJar))
	javaArgs = append(javaArgs, "-jar")
	sandboxHome := path.Join(util.GetLibHome(), "sandbox")
	sandboxLibPath := path.Join(sandboxHome, "lib")
	javaArgs = append(javaArgs, fmt.Sprintf("%s/sandbox-core.jar", sandboxLibPath))
	javaArgs = append(javaArgs, pid)
	javaArgs = append(javaArgs, fmt.Sprintf("%s/sandbox-agent.jar", sandboxLibPath))
	sandboxAttachArgs := fmt.Sprintf("home=%s;token=%s;server.ip=%s;server.port=%s;namespace=%s",
		sandboxHome, token, "127.0.0.1", port, DefaultNamespace)
	javaArgs = append(javaArgs, sandboxAttachArgs)

	return javaArgs
}

func getSandboxToken(ctx context.Context) (string, error) {
	// create sandbox token
	return uuid.NewV4().String(), nil

}

type Charset string

const (
	UTF8    = Charset("UTF-8")
	GB18030 = Charset("GB18030")
)

func ConvertByte2String(byte []byte, charset Charset) string {

	var str string
	switch charset {
	case GB18030:
		decodeBytes, _ := simplifiedchinese.GB18030.NewDecoder().Bytes(byte)
		str = string(decodeBytes)
	case UTF8:
		fallthrough
	default:
		str = string(byte)
	}

	return str
}
