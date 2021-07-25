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
	"context"
	"fmt"
	"github.com/chaosblade-io/chaosblade-exec-os/exec"
	"github.com/chaosblade-io/chaosblade-spec-go/channel"
	"github.com/chaosblade-io/chaosblade-spec-go/spec"
	"github.com/chaosblade-io/chaosblade-spec-go/util"
	"path"
)

type Executor struct {
}

func NewExecutor() spec.Executor {
	return &Executor{}
}

func (*Executor) Name() string {
	return "os"
}

var c = channel.NewLocalChannel()

const (
	OS_BIN  = "chaos_os"
	CREATE  = "create"
	DESTROY = "destroy"
)

func (e *Executor) Exec(uid string, ctx context.Context, model *spec.ExpModel) *spec.Response {

	if model.ActionFlags[exec.ChannelFlag.Name] == "ssh" {
		sshExecutor:= &exec.SSHExecutor{}
		return sshExecutor.Exec(uid, ctx, model)
	}

	var args string
	var flags string
	for k, v := range model.ActionFlags {
		if v == "" {
			continue
		}
		flags = fmt.Sprintf("%s %s=%s", flags, k, v)
	}

	if _, ok := spec.IsDestroy(ctx); ok {
		args = fmt.Sprintf("%s %s %s%s uid=%s", DESTROY, model.Target, model.ActionName, flags, uid)
	} else {
		args = fmt.Sprintf("%s %s %s%s uid=%s", CREATE, model.Target, model.ActionName, flags, uid)
	}

	response := c.Run(ctx, path.Join(util.GetBinPath(), OS_BIN), args)
	if response.Success {
		return spec.Decode(response.Result.(string), response)
	} else {
		return response
	}
}

func (*Executor) SetChannel(channel spec.Channel) {
}
