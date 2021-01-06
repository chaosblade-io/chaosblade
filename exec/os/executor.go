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
	"github.com/chaosblade-io/chaosblade-exec-os/exec/model"
	"github.com/chaosblade-io/chaosblade-spec-go/channel"
	"github.com/chaosblade-io/chaosblade-spec-go/spec"
	"github.com/chaosblade-io/chaosblade-spec-go/util"
)

type Executor struct {
	executors   map[string]spec.Executor
	sshExecutor spec.Executor
}

func NewExecutor() spec.Executor {
	return &Executor{
		executors:   model.GetAllOsExecutors(),
		sshExecutor: model.GetSHHExecutor(),
	}
}

func (*Executor) Name() string {
	return "os"
}

func (e *Executor) Exec(uid string, ctx context.Context, model *spec.ExpModel) *spec.Response {
	if model.ActionFlags[exec.ChannelFlag.Name] == e.sshExecutor.Name() {
		return e.sshExecutor.Exec(uid, ctx, model)
	}

	key := model.Target + model.ActionName
	executor := e.executors[key]
	if executor == nil {
		util.Errorf(uid, util.GetRunFuncName(), fmt.Sprintf(spec.ResponseErr[spec.OsExecutorNotFound].ErrInfo, key))
		return spec.ResponseFailWaitResult(spec.OsExecutorNotFound, fmt.Sprintf(spec.ResponseErr[spec.OsExecutorNotFound].Err, uid),
			fmt.Sprintf(spec.ResponseErr[spec.OsExecutorNotFound].ErrInfo, key))
	}
	executor.SetChannel(channel.NewLocalChannel())
	return executor.Exec(uid, ctx, model)
}

func (*Executor) SetChannel(channel spec.Channel) {
}
