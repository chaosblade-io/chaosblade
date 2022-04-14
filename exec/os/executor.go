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

package os

import (
	"bytes"
	"context"
	"fmt"
	"github.com/chaosblade-io/chaosblade-exec-os/exec"
	"github.com/chaosblade-io/chaosblade-spec-go/log"
	"github.com/chaosblade-io/chaosblade-spec-go/spec"
	"github.com/chaosblade-io/chaosblade-spec-go/util"
	os_exec "os/exec"
	"path"
	"strings"
	"syscall"
)

type Executor struct {
}

func NewExecutor() spec.Executor {
	return &Executor{}
}

func (*Executor) Name() string {
	return "os"
}

func (e *Executor) Exec(uid string, ctx context.Context, model *spec.ExpModel) *spec.Response {

	if model.ActionFlags[exec.ChannelFlag.Name] == "ssh" {
		sshExecutor := &exec.SSHExecutor{}
		return sshExecutor.Exec(uid, ctx, model)
	}

	var args string
	var flags string
	for k, v := range model.ActionFlags {
		if v == "" ||  k == "timeout" {
			continue
		}
		flags = fmt.Sprintf("%s --%s=%s", flags, k, v)
	}

	_, isDestroy := spec.IsDestroy(ctx)

	if isDestroy {
		args = fmt.Sprintf("%s %s %s%s uid=%s", spec.Destroy, model.Target, model.ActionName, flags, uid)
	} else {
		args = fmt.Sprintf("%s %s %s%s uid=%s", spec.Create, model.Target, model.ActionName, flags, uid)
	}
	chaosOsBin := path.Join(util.GetProgramPath(), "bin", spec.ChaosOsBin)
	argsArray := strings.Split(args, " ")
	command := os_exec.CommandContext(ctx, chaosOsBin, argsArray...)
	log.Debugf(ctx, "run command, %s %s", chaosOsBin, args)

	if model.ActionProcessHang && !isDestroy {
		if err := command.Start(); err != nil {
			sprintf := fmt.Sprintf("create experiment command start failed, %v", err)
			return spec.ReturnFail(spec.OsCmdExecFailed, sprintf)
		}
		command.SysProcAttr = &syscall.SysProcAttr{}
		return spec.ReturnSuccess(command.Process.Pid)
	} else {
		buf := new(bytes.Buffer)
		command.Stdout = buf
		command.Stderr = buf
		if err := command.Start(); err != nil {
			sprintf := fmt.Sprintf("create experiment command start failed, %v", err)
			return spec.ReturnFail(spec.OsCmdExecFailed, sprintf)
		}

		if err := command.Wait(); err != nil {
			sprintf := fmt.Sprintf("create experiment command wait failed, %s", err.Error())
			log.Debugf(ctx, "command result: %s, err: %s", buf.String(), err.Error())
			if buf.Len() > 0  {
				return spec.ReturnFail(spec.OsCmdExecFailed, buf.String())
			}
			return spec.ReturnFail(spec.OsCmdExecFailed, sprintf)
		}
		log.Debugf(ctx, "command result: %s", buf.String())
		return spec.Decode(buf.String(), nil)
	}
}

func (*Executor) SetChannel(channel spec.Channel) {
}
