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
	"encoding/json"
	"fmt"
	"strings"

	specchannel "github.com/chaosblade-io/chaosblade-spec-go/channel"
	"github.com/chaosblade-io/chaosblade-spec-go/spec"
	"github.com/chaosblade-io/chaosblade-spec-go/util"
	"github.com/sirupsen/logrus"

	"github.com/chaosblade-io/chaosblade/data"
)

const DefaultUri = "sandbox/default/module/http/chaosblade"

// Executor for jvm experiment
type Executor struct {
	Uri     string
	channel spec.Channel
}

//var log = logf.Log.WithName("jvm")

func NewExecutor() *Executor {
	return &Executor{
		Uri:     DefaultUri,
		channel: specchannel.NewLocalChannel(),
	}
}

func (e *Executor) Name() string {
	return "jvm"
}

func (e *Executor) SetChannel(channel spec.Channel) {
	e.channel = channel
}

func (e *Executor) Exec(uid string, ctx context.Context, model *spec.ExpModel) *spec.Response {
	var url_ string
	port, err := e.getPortFromDB(model.ActionFlags["process"], "")
	if err != nil {
		return spec.ReturnFail(spec.Code[spec.ServerError], "cannot get port from local, please execute prepare command first")
	}
	var result string
	var code int
	if suid, ok := spec.IsDestroy(ctx); ok {
		if suid == spec.UnknownUid {
			url_ = e.sandboxUrl(port, e.getDestroyRequestPathWithoutUid(model.Target, model.ActionName))
		} else {
			url_ = e.sandboxUrl(port, e.getDestroyRequestPathWithUid(uid))
		}
		result, err, code = util.Curl(url_)
	} else {
		var body []byte
		url_, body, err = e.createUrl(port, uid, model)
		if err != nil {
			return spec.ReturnFail(spec.Code[spec.ServerError], err.Error())
		}
		result, err, code = util.PostCurl(url_, body)
	}
	if err != nil {
		return spec.ReturnFail(spec.Code[spec.SandboxInvokeError], err.Error())
	}
	if code == 404 {
		return spec.ReturnFail(spec.Code[spec.JavaAgentCmdError], "please execute prepare command first")
	}
	if code == 200 {
		var resp spec.Response
		err := json.Unmarshal([]byte(result), &resp)
		if err != nil {
			return spec.ReturnFail(spec.Code[spec.SandboxInvokeError],
				fmt.Sprintf("unmarshal create command result %s err, %v", result, err))
		}
		return &resp
	}
	return spec.ReturnFail(spec.Code[spec.SandboxInvokeError],
		fmt.Sprintf("response code is %d, result: %s", code, result))
}

func (e *Executor) createUrl(port, suid string, model *spec.ExpModel) (string, []byte, error) {
	url := e.sandboxUrl(port, "create")
	bodyMap := make(map[string]string, 0)
	bodyMap["target"] = model.Target
	bodyMap["suid"] = suid
	bodyMap["action"] = model.ActionName

	for k, v := range model.ActionFlags {
		if v == "" || v == "false" {
			continue
		}
		// filter timeout because of the java agent implementation by all matchers
		if k == "timeout" {
			continue
		}
		bodyMap[k] = v
	}
	// encode
	bytes, err := json.Marshal(bodyMap)
	if err != nil {
		logrus.Warningf("Marshal request body to json error. %v", err)
		return "", nil, err
	}
	return url, bytes, nil
}

func (e *Executor) sandboxUrl(port, requestPath string) string {
	return fmt.Sprintf("http://%s:%s/%s/%s", "127.0.0.1", port, e.Uri, requestPath)
}

func (e *Executor) getDestroyRequestPathWithUid(uid string) string {
	return fmt.Sprintf("destroy?suid=%s", uid)
}

func (e *Executor) getDestroyRequestPathWithoutUid(target string, action string) string {
	return fmt.Sprintf("destroy?target=%s&action=%s", target, action)
}

func (e *Executor) getStatusRequestPath(uid string) string {
	return fmt.Sprintf("status?suid=%s", uid)
}

func (e *Executor) QueryStatus(uid string) *spec.Response {
	experimentModel, err := db.QueryExperimentModelByUid(uid)
	if err != nil {
		return spec.ReturnFail(spec.Code[spec.DatabaseError],
			fmt.Sprintf("query experiment error, %s", err.Error()))
	}
	if experimentModel == nil {
		return spec.ReturnFail(spec.Code[spec.DataNotFound], "the experiment record not found")
	}
	// get process flag
	process := getProcessFlagFromExpRecord(experimentModel)
	port, err := e.getPortFromDB(process, "")
	if err != nil {
		return spec.ReturnFail(spec.Code[spec.SandboxInvokeError], err.Error())
	}
	url := e.sandboxUrl(port, e.getStatusRequestPath(uid))
	result, err, code := util.Curl(url)
	if err != nil {
		return spec.ReturnFail(spec.Code[spec.SandboxInvokeError], err.Error())
	}
	if code == 404 {
		return spec.ReturnFail(spec.Code[spec.SandboxInvokeError], "the command not support")
	}
	if code != 200 {
		return spec.ReturnFail(spec.Code[spec.SandboxInvokeError],
			fmt.Sprintf("query response code is %d, result: %s", code, result))
	}
	var resp spec.Response
	err = json.Unmarshal([]byte(result), &resp)
	if err != nil {
		return spec.ReturnFail(spec.Code[spec.SandboxInvokeError],
			fmt.Sprintf("unmarshal query command result %s err, %v", result, err))
	}
	return &resp
}

var db = data.GetSource()

func (e *Executor) getPortFromDB(processName, processId string) (string, error) {
	if processName != "" || processId != "" {
		pid, response := CheckFlagValues(processName, processId)
		if !response.Success {
			return "", fmt.Errorf(response.Err)
		}
		processId = pid
	}
	record, err := db.QueryRunningPreByTypeAndProcess("jvm", processName, processId)
	if err != nil {
		return "", err
	}
	if record == nil {
		return "", fmt.Errorf("port not found for %s process, please execute prepare command firstly", processName)
	}
	return record.Port, nil
}

func getProcessFlagFromExpRecord(model *data.ExperimentModel) string {
	flagValue := model.Flag
	fields := strings.Fields(flagValue)
	for idx, value := range fields {
		if strings.HasPrefix(value, "--process") || strings.HasPrefix(value, "-process") {
			// contains process flag, predicate equal symbol next
			eqlIdx := strings.Index(value, "=")
			if eqlIdx > 0 {
				return value[eqlIdx+1:]
			}
			return fields[idx+1]
		}
	}
	return ""
}

// checkFlagValues
// query pre-record from sqlite by process name or process id
// 1. The process and pid are not empty, then the process is used to find the process. If the process id and the found process are not found, the error is returned.
// 2. Process is empty, pid is not empty, then determine if the pid process exists
// 3. Process is not empty, pid is empty, then it is judged whether the process exists, there is no error, and the process id is assigned to pid.
// 4. Process and pid are both empty, then an error is returned.
func CheckFlagValues(processName, processId string) (string, *spec.Response) {
	if processName == "" {
		exists, err := specchannel.ProcessExists(processId)
		if err != nil {
			return processId, spec.ReturnFail(spec.Code[spec.GetProcessError],
				fmt.Sprintf("the %s process id doesn't exist, %s", processId, err.Error()))
		}
		if !exists {
			return processId, spec.ReturnFail(spec.Code[spec.IllegalParameters],
				fmt.Sprintf("the %s process id doesn't exist.", processId))
		}
	}
	if processName != "" {
		ctx := context.WithValue(context.Background(), specchannel.ProcessKey, "java")
                // set pecchannel.ExcludeProcessKey as "blade" to exclude pid of the blade command we run when querying the target application by processName
                // If ExcludeProcessKey is not set, multiple pids might be returned (the blade command pid might be one of the pids.)
                ctx = context.WithValue(ctx, specchannel.ExcludeProcessKey, "blade")
		pids, err := specchannel.GetPidsByProcessName(processName, ctx)
		if err != nil {
			return processId, spec.ReturnFail(spec.Code[spec.GetProcessError], err.Error())
		}
		if pids == nil || len(pids) == 0 {
			return processId, spec.ReturnFail(spec.Code[spec.GetProcessError], "process not found")
		}
		if len(pids) == 1 {
			if processId == "" {
				processId = pids[0]
			} else if processId != pids[0] {
				return processId, spec.ReturnFail(spec.Code[spec.IllegalParameters],
					fmt.Sprintf("get process id by process name is %s, not equal the value of pid flag", pids[0]))
			}
		} else {
			if processId == "" {
				return processId, spec.ReturnFail(spec.Code[spec.GetProcessError], "too many process")
			} else {
				var contains bool
				for _, p := range pids {
					if p == processId {
						contains = true
						break
					}
				}
				if !contains {
					return processId, spec.ReturnFail(spec.Code[spec.IllegalParameters],
						"the process ids got by process name does not contain the pid value")
				}
			}
		}
	}
	return processId, spec.ReturnSuccess("success")
}
