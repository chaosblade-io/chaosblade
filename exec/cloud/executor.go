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

package cloud

import (
	"context"
	"fmt"
	"github.com/chaosblade-io/chaosblade-exec-cloud/exec"
	"github.com/chaosblade-io/chaosblade-spec-go/log"
	"github.com/chaosblade-io/chaosblade-spec-go/spec"
	"github.com/chaosblade-io/chaosblade-spec-go/util"
	os_exec "os/exec"
	"path"
	"syscall"
)

type Executor struct {
}

func NewExecutor() spec.Executor {
	return &Executor{}
}

func (*Executor) Name() string {
	return "cloud"
}

func (e *Executor) Exec(uid string, ctx context.Context, model *spec.ExpModel) *spec.Response {

	if model.ActionFlags[exec.ChannelFlag.Name] == "ssh" {
		sshExecutor := &exec.SSHExecutor{}
		return sshExecutor.Exec(uid, ctx, model)
	}

	var mode string
	var argsArray []string

	_, isDestroy := spec.IsDestroy(ctx)
	if isDestroy {
		mode = spec.Destroy
	} else {
		mode = spec.Create
	}

	argsArray = append(argsArray, mode, model.Target, model.ActionName, fmt.Sprintf("--uid=%s", uid))
	for k, v := range model.ActionFlags {
		if v == "" || k == "timeout" {
			continue
		}
		argsArray = append(argsArray, fmt.Sprintf("--%s=%s", k, v))
	}

	chaosCloudBin := path.Join(util.GetProgramPath(), "bin", spec.ChaosCloudBin)
	command := os_exec.CommandContext(ctx, chaosCloudBin, argsArray...)
	log.Debugf(ctx, "run command, %s %v", chaosCloudBin, argsArray)

	if model.ActionProcessHang && !isDestroy {
		if err := command.Start(); err != nil {
			sprintf := fmt.Sprintf("create experiment command start failed, %v", err)
			return spec.ReturnFail(spec.OsCmdExecFailed, sprintf)
		}
		command.SysProcAttr = &syscall.SysProcAttr{}
		return spec.ReturnSuccess(command.Process.Pid)
	} else {
		output, err := command.CombinedOutput()
		outMsg := string(output)
		log.Debugf(ctx, "Command Result, output: %v, err: %v", outMsg, err)
		if err != nil {
			return spec.ReturnFail(spec.OsCmdExecFailed, fmt.Sprintf("command exec failed, %s", err.Error()))
		}
		return spec.Decode(outMsg, nil)
	}
}

func (*Executor) SetChannel(channel spec.Channel) {
}
