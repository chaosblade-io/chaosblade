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
	"strings"

	"github.com/chaosblade-io/chaosblade-exec-os/exec"
	"github.com/chaosblade-io/chaosblade-spec-go/spec"
)

type ComposeExecutor interface {
	spec.Executor
}

type ComposeExecutorForJvm struct {
	localExecutor spec.Executor
	sshExecutor     spec.Executor
}

func NewComposeExecutor() ComposeExecutor {
	return &ComposeExecutorForJvm{
		localExecutor: NewExecutor(),
		sshExecutor: exec.NewSSHExecutor(),
	}
}

func (*ComposeExecutorForJvm) Name() string {
	return "os"
}

func (e *ComposeExecutorForJvm) Exec(uid string, ctx context.Context, model *spec.ExpModel) *spec.Response {
	if strings.ToLower(model.ActionFlags[exec.ChannelFlag.Name]) == e.sshExecutor.Name() {
		return e.sshExecutor.Exec(uid, ctx, model)
	} else {
		return e.localExecutor.Exec(uid, ctx, model)
	}
}

func (*ComposeExecutorForJvm) SetChannel(channel spec.Channel) {

}
