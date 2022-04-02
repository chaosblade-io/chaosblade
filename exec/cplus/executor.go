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
	"github.com/chaosblade-io/chaosblade-spec-go/log"
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
	port, resp := e.getPortFromDB(ctx, uid, model)
	if resp != nil {
		return resp
	}

	if _, ok := spec.IsDestroy(ctx); ok {
		url = e.destroyUrl(port, uid)
	} else {
		url = e.createUrl(port, uid, model)
	}
	result, err, code := util.Curl(ctx, url)
	if err != nil {
		log.Errorf(ctx, spec.HttpExecFailed.Sprintf(url, err))
		return spec.ResponseFailWithFlags(spec.HttpExecFailed, url, err)
	}
	if code == 200 {
		var resp spec.Response
		err := json.Unmarshal([]byte(result), &resp)
		if err != nil {
			log.Errorf(ctx, spec.ResultUnmarshalFailed.Sprintf(result, err))
			return spec.ResponseFailWithFlags(spec.ResultUnmarshalFailed, result, err)
		}
		return &resp
	}
	log.Errorf(ctx, spec.HttpExecFailed.Sprintf(url, result))
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

var db = data.GetSource()

func (e *Executor) getPortFromDB(ctx context.Context, uid string, model *spec.ExpModel) (string, *spec.Response) {
	port := model.ActionFlags["port"]
	record, err := db.QueryRunningPreByTypeAndProcess("cplus", port, "")
	if err != nil {
		log.Errorf(ctx, spec.DatabaseError.Sprintf("query", err))
		return "", spec.ResponseFailWithFlags(spec.DatabaseError, "query", err)
	}
	if record == nil {
		log.Errorf(ctx, spec.ParameterInvalidCplusPort.Sprintf(port))
		return "", spec.ResponseFailWithFlags(spec.ParameterInvalidCplusPort, port)
	}
	return record.Port, nil
}
