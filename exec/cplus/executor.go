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
	neturl "net/url"

	"github.com/chaosblade-io/chaosblade-spec-go/channel"
	"github.com/chaosblade-io/chaosblade-spec-go/spec"
	"github.com/chaosblade-io/chaosblade-spec-go/util"

	"github.com/chaosblade-io/chaosblade/data"
)

// Executor for jvm experiment
type Executor struct {
	Uri     string
	channel spec.Channel
}

func NewExecutor() *Executor {
	return &Executor{
		channel: channel.NewLocalChannel(),
	}
}

func (e *Executor) Name() string {
	return "cplus"
}

func (e *Executor) SetChannel(channel spec.Channel) {
	e.channel = channel
}

func (e *Executor) Exec(uid string, ctx context.Context, model *spec.ExpModel) *spec.Response {
	var url string
	port, err := e.getPortFromDB(model)
	if err != nil {
		return spec.ReturnFail(spec.Code[spec.ServerError], "cannot get port from local")
	}
	if _, ok := spec.IsDestroy(ctx); ok {
		url = e.destroyUrl(port, uid)
	} else {
		url = e.createUrl(port, uid, model)
	}
	result, err, code := util.Curl(url)
	if err != nil {
		return spec.ReturnFail(spec.Code[spec.CplusProxyCmdError], err.Error())
	}
	if code == 200 {
		var resp spec.Response
		err := json.Unmarshal([]byte(result), &resp)
		if err != nil {
			return spec.ReturnFail(spec.Code[spec.CplusProxyCmdError],
				fmt.Sprintf("unmarshal create command result %s err, %v", result, err))
		}
		return &resp
	}
	return spec.ReturnFail(spec.Code[spec.CplusProxyCmdError],
		fmt.Sprintf("response code is %d, result: %s", code, result))
}

func (e *Executor) createUrl(port, suid string, model *spec.ExpModel) string {
	url := fmt.Sprintf("http://%s:%s/create?target=%s&suid=%s&action=%s",
		"127.0.0.1", port, model.Target, suid, model.ActionName)
	for k, v := range model.ActionFlags {
		if v == "" || v == "false" {
			continue
		}
		// filter timeout because of the agent implementation by all matchers
		if k == "timeout" {
			continue
		}
		url = fmt.Sprintf("%s&%s=%s", url, k, neturl.QueryEscape(v))
	}
	return url
}

func (e *Executor) destroyUrl(port, uid string) string {
	url := fmt.Sprintf("http://%s:%s/destroy?suid=%s",
		"127.0.0.1", port, uid)
	return url
}

var db = data.GetSource()

func (e *Executor) getPortFromDB(model *spec.ExpModel) (string, error) {
	port := model.ActionFlags["port"]
	record, err := db.QueryRunningPreByTypeAndProcess("cplus", port, "")
	if err != nil {
		return "", err
	}
	if record == nil {
		return "", fmt.Errorf("%s port not found, please execute prepare command firstly", port)
	}
	return record.Port, nil
}
