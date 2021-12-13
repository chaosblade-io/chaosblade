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
	port, resp := e.getPortFromDB(uid, model)
	if resp != nil {
		return resp
	}

	if _, ok := spec.IsDestroy(ctx); ok {
		url = e.destroyUrl(port, uid)
	} else {
		url = e.createUrl(port, uid, model)
	}
	result, err, code := util.Curl(url)
	if err != nil {
		util.Errorf(uid, util.GetRunFuncName(), spec.HttpExecFailed.Sprintf(url, err))
		return spec.ResponseFailWithFlags(spec.HttpExecFailed, url, err)
	}
	if code == 200 {
		var resp spec.Response
		err := json.Unmarshal([]byte(result), &resp)
		if err != nil {
			util.Errorf(uid, util.GetRunFuncName(), spec.ResultUnmarshalFailed.Sprintf(result, err))
			return spec.ResponseFailWithFlags(spec.ResultUnmarshalFailed, result, err)
		}
		return &resp
	}
	util.Errorf(uid, util.GetRunFuncName(), spec.HttpExecFailed.Sprintf(url, result))
	return spec.ResponseFailWithFlags(spec.HttpExecFailed, url, result)
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

//取消直接调用 data.GetSource() 的方式，会导致 GetSource() 在运行 cobra.Command 之前执行而无法获取到 flag 参数。由于不能和 cmd package
//循环依赖所以没法使用 cmd.GetDS()，且没有该工程中没有公共的 util，所以每个用到 data source 的地方都需要重复一遍 GetDS()，所以更好的方式
//应该是把 data package 的部分放到 spec-go 里面去，并在 spec-go 里面提供 util.GetDS() 来使用
//var db = data.GetSource()
var ds data.SourceI

// GetDS returns dataSource
func GetDS() data.SourceI {
	if ds == nil {
		ds = data.GetSource()
	}
	return ds
}

func (e *Executor) getPortFromDB(uid string, model *spec.ExpModel) (string, *spec.Response) {
	port := model.ActionFlags["port"]
	record, err := GetDS().QueryRunningPreByTypeAndProcess("cplus", port, "")
	if err != nil {
		util.Errorf(uid, util.GetRunFuncName(), spec.DatabaseError.Sprintf("query", err))
		return "", spec.ResponseFailWithFlags(spec.DatabaseError, "query", err)
	}
	if record == nil {
		util.Errorf(uid, util.GetRunFuncName(), spec.ParameterInvalidCplusPort.Sprintf(port))
		return "", spec.ResponseFailWithFlags(spec.ParameterInvalidCplusPort, port)
	}
	return record.Port, nil
}
