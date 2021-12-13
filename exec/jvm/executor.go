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
	"strconv"
	"strings"
	"time"

	"github.com/chaosblade-io/chaosblade-spec-go/channel"
	"github.com/chaosblade-io/chaosblade-spec-go/spec"
	"github.com/chaosblade-io/chaosblade-spec-go/util"
	"github.com/sirupsen/logrus"

	"github.com/chaosblade-io/chaosblade/data"
)

const DefaultUri = "sandbox/" + DefaultNamespace + "/module/http/chaosblade"

// Executor for jvm experiment
type Executor struct {
	Uri     string
	channel spec.Channel
}

//var log = logf.Log.WithName("jvm")

func NewExecutor() *Executor {
	return &Executor{
		Uri:     DefaultUri,
		channel: channel.NewLocalChannel(),
	}
}

func (e *Executor) Name() string {
	return "jvm"
}

func (e *Executor) SetChannel(channel spec.Channel) {
	e.channel = channel
}

func (e *Executor) Exec(uid string, ctx context.Context, model *spec.ExpModel) *spec.Response {
	// 1. check parameters
	processName := model.ActionFlags["process"]
	processId := model.ActionFlags["pid"]
	// refresh flag is used to reload java agent
	refresh := model.ActionFlags["refresh"] == "true"
	javaHome := model.ActionFlags["javaHome"]

	// 2. get record from db by processname|processId
	suid, isDestroy := spec.IsDestroy(ctx)
	record, err := e.getRecordFromDB(uid, processName, processId)
	if err != nil {
		util.Errorf(uid, util.GetRunFuncName(), spec.DatabaseError.Sprintf("get",
			fmt.Sprintf("where by processName:%s or pid%s", processName, processId), err.Error()))
		return spec.ResponseFailWithFlags(spec.DatabaseError, "get",
			fmt.Sprintf("where by processName:%s or pid%s", processName, processId), err.Error())
	}
	var port, pid string
	if record != nil {
		port = record.Port
		pid = record.Pid
	}

	// 3. exec command
	if isDestroy {
		if port == "" {
			if suid == spec.UnknownUid {
				if processName == "" && processId == "" {
					util.Errorf(uid, util.GetRunFuncName(), spec.ParameterLess.Sprintf("process|pid"))
					return spec.ResponseFailWithFlags(spec.ParameterLess, "process|pid")
				}
			}
			if processName == "" && processId == "" {
				return spec.ReturnSuccess(fmt.Sprintf("no prepare record, uid: %s", suid))
			}
			processId, response := CheckFlagValues(uid, processName, processId)
			if !response.Success {
				return response
			}
			username, err := getUsername(processId)
			if err != nil {
				util.Errorf(uid, util.GetRunFuncName(), spec.ProcessGetUsernameFailed.Sprintf(processId, err))
				return spec.ResponseFailWithFlags(spec.ProcessGetUsernameFailed, processId, err)
			}
			// get port from sandbox.token
			port, err = getPortFromSandboxToken(username)
			if err != nil {
				return spec.ReturnSuccess(fmt.Sprintf("no record, %v", err))
			}
		}
	} else {
		if port == "" || err != nil {
			logrus.Warn(fmt.Sprintf("select record fail, uid: %s, err: %v", uid, err))
			if processName == "" && processId == "" {
				util.Errorf(uid, util.GetRunFuncName(), spec.ParameterLess.Sprintf("process|pid"))
				return spec.ResponseFailWithFlags(spec.ParameterLess, "process|pid")
			}
		}
		if refresh {
			// Uninstall java agent
			logrus.Info("Uninstall java agent")
			response := Revoke(uid, record, processName, processId)
			if !response.Success {
				return response
			}
		}

		// jvm restart case
		if exists, _ := cl.ProcessExists(pid); !exists && port != "" {
			logrus.Infof("pid %s not exists", pid)
			port = ""
		}

		// Install java agent
		if port == "" || refresh {
			logrus.Info("Install java agent")
			response, newPort := Prepare(uid, processName, processId, javaHome)
			if !response.Success {
				return response
			}
			port = newPort
		}
	}

	var result string
	var code int
	var url string
	if isDestroy {
		if suid == spec.UnknownUid {
			url = e.sandboxUrl(port, e.getDestroyRequestPathWithoutUid(model.Target, model.ActionName))
		} else {
			url = e.sandboxUrl(port, e.getDestroyRequestPathWithUid(uid))
		}
		result, err, code = util.Curl(url)
	} else {
		var body []byte
		url, body, resp := e.createUrl(port, uid, model)
		if err != nil {
			return resp
		}
		result, err, code = util.PostCurl(url, body, "")
	}
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

func (e *Executor) createUrl(port, suid string, model *spec.ExpModel) (string, []byte, *spec.Response) {
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
		util.Warnf(suid, util.GetRunFuncName(), spec.ResultMarshalFailed.Sprintf(bodyMap, err))
		return "", nil, spec.ResponseFailWithFlags(spec.ResultMarshalFailed, bodyMap, err)
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
	experimentModel, err := GetDS().QueryExperimentModelByUid(uid)
	if err != nil {
		util.Errorf(uid, util.GetRunFuncName(), spec.DatabaseError.Sprintf("query", err))
		return spec.ResponseFailWithFlags(spec.DatabaseError, "query", err)
	}
	if experimentModel == nil {
		util.Errorf(uid, util.GetRunFuncName(), spec.DataNotFound.Sprintf(uid))
		return spec.ResponseFailWithFlags(spec.DataNotFound, uid)
	}
	// get process flag
	process := getProcessFlagFromExpRecord(experimentModel)
	record, err := e.getRecordFromDB(uid, process, "")
	if err != nil {
		util.Errorf(uid, util.GetRunFuncName(), spec.DatabaseError.Sprintf("query", err))
		return spec.ResponseFailWithFlags(spec.DatabaseError, "query", err)
	}
	if record == nil {
		util.Errorf(uid, util.GetRunFuncName(), spec.DataNotFound.Sprintf(uid))
		return spec.ResponseFailWithFlags(spec.DataNotFound, uid)
	}
	port := record.Port
	url := e.sandboxUrl(port, e.getStatusRequestPath(uid))
	result, err, code := util.Curl(url)
	if err != nil {
		util.Errorf(uid, util.GetRunFuncName(), spec.HttpExecFailed.Sprintf(url, err))
		return spec.ResponseFailWithFlags(spec.HttpExecFailed, url, err)
	}
	if code != 200 {
		util.Errorf(uid, util.GetRunFuncName(), spec.HttpExecFailed.Sprintf(url, result))
		return spec.ResponseFailWithFlags(spec.HttpExecFailed, url, result)
	}
	var resp spec.Response
	err = json.Unmarshal([]byte(result), &resp)
	if err != nil {
		util.Errorf(uid, util.GetRunFuncName(), spec.ResultUnmarshalFailed.Sprintf(result, err))
		return spec.ResponseFailWithFlags(spec.ResultUnmarshalFailed, result, err)
	}
	return &resp
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

func (e *Executor) getRecordFromDB(uid, processName, processId string) (*data.PreparationRecord, error) {
	if processName != "" || processId != "" {
		pid, response := CheckFlagValues(uid, processName, processId)
		if !response.Success {
			return nil, fmt.Errorf(response.Err)
		}
		processId = pid
	}
	record, err := GetDS().QueryRunningPreByTypeAndProcess("jvm", processName, processId)
	if err != nil {
		return nil, err
	}
	return record, nil
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
func CheckFlagValues(uid, processName, processId string) (string, *spec.Response) {
	cl := channel.NewLocalChannel()
	if processName == "" {
		if processId == "" {
			util.Errorf(uid, util.GetRunFuncName(), spec.ParameterLess.Sprintf("process|pid"))
			return "", spec.ResponseFailWithFlags(spec.ParameterLess, "process|pid")
		}
		exists, err := cl.ProcessExists(processId)
		if err != nil {
			util.Errorf(uid, util.GetRunFuncName(), spec.ProcessJudgeExistFailed.Sprintf(processId, err))
			return "", spec.ResponseFailWithFlags(spec.ProcessJudgeExistFailed, processId, err)
		}
		if !exists {
			util.Errorf(uid, util.GetRunFuncName(), spec.ParameterInvalidProName.Sprintf("pid", processId))
			return "", spec.ResponseFailWithFlags(spec.ParameterInvalidProName, "pid", processId)
		}
	}
	if processName != "" {
		ctx := context.WithValue(context.Background(), channel.ProcessCommandKey, "java")
		// set pecchannel.ExcludeProcessKey as "blade" to exclude pid of the blade command we run when querying the target application by processName
		// If ExcludeProcessKey is not set, multiple pids might be returned (the blade command pid might be one of the pids.)
		ctx = context.WithValue(ctx, channel.ExcludeProcessKey, "blade")
		pids, err := cl.GetPidsByProcessName(processName, ctx)
		if err != nil {
			util.Errorf(uid, util.GetRunFuncName(), spec.ProcessIdByNameFailed.Sprintf(processName, err))
			return "", spec.ResponseFailWithFlags(spec.ProcessIdByNameFailed, processName, err)
		}
		if pids == nil || len(pids) == 0 {
			util.Errorf(uid, util.GetRunFuncName(), spec.ParameterInvalidProName.Sprintf("process", processName))
			return "", spec.ResponseFailWithFlags(spec.ParameterInvalidProName, "process", processName)
		}
		if len(pids) == 1 {
			if processId == "" {
				processId = pids[0]
			} else if processId != pids[0] {
				util.Errorf(uid, util.GetRunFuncName(), spec.ParameterInvalidProIdNotByName.Sprintf(processName, processId))
				return "", spec.ResponseFailWithFlags(spec.ParameterInvalidProIdNotByName, processName, processId)
			}
		} else {
			if processId == "" {
				util.Errorf(uid, util.GetRunFuncName(), spec.ParameterInvalidTooManyProcess.Sprintf(processName))
				return "", spec.ResponseFailWithFlags(spec.ParameterInvalidTooManyProcess, processName)
			} else {
				var contains bool
				for _, p := range pids {
					if p == processId {
						contains = true
						break
					}
				}
				if !contains {
					util.Errorf(uid, util.GetRunFuncName(), spec.ParameterInvalidProIdNotByName.Sprintf(processName, processId))
					return "", spec.ResponseFailWithFlags(spec.ParameterInvalidProIdNotByName, processName, processId)
				}
			}
		}
	}
	return processId, spec.ReturnSuccess("success")
}

func Prepare(uid, processName, processId, javaHome string) (response *spec.Response, port string) {
	processId, response = CheckFlagValues(uid, processName, processId)
	if !response.Success {
		return
	}
	record, err := GetDS().QueryRunningPreByTypeAndProcess("jvm", processName, processId)
	if record == nil || err != nil || record.Uid == "" {
		// get port from local port
		port, err = getAndCacheSandboxPort()
		if err != nil {
			util.Errorf(uid, util.GetRunFuncName(), spec.SandboxGetPortFailed.Sprintf(err))
			return spec.ResponseFailWithFlags(spec.SandboxGetPortFailed, err), port
		}
		record, err = insertPrepareRecord("jvm", processName, port, processId)
		if err != nil {
			util.Errorf(uid, util.GetRunFuncName(), spec.DatabaseError.Sprintf("insert", err))
			return spec.ResponseFailWithFlags(spec.DatabaseError, "insert", err), port
		}
	}
	var username string
	port = record.Port
	response, username = Attach(uid, port, javaHome, processId)
	if !response.Success && username != "" && strings.Contains(response.Err, "connection refused") {
		// if attach failed, search port from ~/.sandbox.token
		port, err = CheckPortFromSandboxToken(username)
		if err == nil {
			logrus.Infof("use %s port to retry", port)
			response, username = Attach(uid, port, "", processId)
			if response.Success {
				// update port
				err := GetDS().UpdatePreparationPortByUid(record.Uid, port)
				if err != nil {
					logrus.Warningf("update preparation port failed, %v", err)
				}
			}
		}
	}
	if record.Pid != processId {
		// update pid
		GetDS().UpdatePreparationPidByUid(record.Uid, processId)
	}
	handlePrepareResponse(record.Uid, response)
	return response, port
}

// Revoke 卸载 Java agent
func Revoke(uid string, record *data.PreparationRecord, processName, processId string) *spec.Response {
	var port string
	if record == nil {
		processId, response := CheckFlagValues(uid, processName, processId)
		if !response.Success {
			return response
		}
		username, err := getUsername(processId)
		if err != nil {
			util.Errorf(uid, util.GetRunFuncName(), spec.ProcessGetUsernameFailed.Sprintf(processId, err))
			return spec.ResponseFailWithFlags(spec.ProcessGetUsernameFailed, processId, err)
		}
		// get port from sandbox.token
		port, err = getPortFromSandboxToken(username)
		if err != nil {
			return spec.ReturnSuccess("no record")
		}
	} else {
		if record.Status == "Revoked" {
			return spec.ReturnSuccess("success")
		}
		port = record.Port
	}
	if response := Detach(uid, port); !response.Success {
		logrus.WithFields(logrus.Fields{
			"processName": processName,
			"processId":   processId,
		}).Warningln(response.Print())
	}
	// TODO 默认成功，不影响后续执行
	return spec.ReturnSuccess("success")
}

// getSandboxPort by process name. If this process does not exist, an unbound port will be selected
func getAndCacheSandboxPort() (string, error) {
	port, err := util.GetUnusedPort()
	if err != nil {
		return "", err
	}
	return strconv.Itoa(port), nil
}

// insertPrepareRecord
func insertPrepareRecord(prepareType string, processName, port, processId string) (*data.PreparationRecord, error) {
	uid, err := util.GenerateUid()
	if err != nil {
		return nil, err
	}
	record := &data.PreparationRecord{
		Uid:         uid,
		ProgramType: prepareType,
		Process:     processName,
		Port:        port,
		Pid:         processId,
		Status:      "Created",
		Error:       "",
		CreateTime:  time.Now().Format(time.RFC3339Nano),
		UpdateTime:  time.Now().Format(time.RFC3339Nano),
	}
	err = GetDS().InsertPreparationRecord(record)
	if err != nil {
		return nil, err
	}
	return record, nil
}

func handlePrepareResponse(uid string, response *spec.Response) {
	response.Result = uid
	if !response.Success {
		GetDS().UpdatePreparationRecordByUid(uid, "Error", response.Err)
		return
	}
	err := GetDS().UpdatePreparationRecordByUid(uid, "Running", "")
	if err != nil {
		logrus.Warningf("update preparation record error: %s", err.Error())
	}
}
