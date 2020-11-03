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
	var url_ string
	processName := model.ActionFlags["process"]
	processId := model.ActionFlags["pid"]
	override := model.ActionFlags["override"] == "true"
	suid, isDestroy := spec.IsDestroy(ctx)
	record, err := e.getRecordFromDB(processName, processId)
	if record == nil || err != nil {
		logrus.Warn(fmt.Sprintf("select record fail, uid: %s, err: %v", uid, err))
		if processName == "" && processId == "" {
			return spec.ReturnFail(spec.Code[spec.IllegalParameters],
				fmt.Sprintf("less --process or --pid flags or can't found record, uid: %s", uid))
		}
	}

	var port string
	if record != nil {
		port = record.Port
	}

	if isDestroy {
		if port == "" {
			processId, response := CheckFlagValues(processName, processId)
			if !response.Success {
				return response
			}
			username, err := getUsername(processId)
			if err != nil {
				return spec.ReturnFail(spec.Code[spec.StatusError],
					fmt.Sprintf("get username failed by %s pid, %v", username, err))
			}
			// get port from sandbox.token
			port, err = getPortFromSandboxToken(username)
			if err != nil {
				return spec.ReturnSuccess(fmt.Sprintf("no record, %v", err))
			}
		}
	} else {
		if override {
			// Uninstall java agent
			logrus.Info("Uninstall java agent")
			response := Revoke(record, processName, processId)
			if !response.Success {
				return response
			}
		}
		// Install java agent
		if port == "" || override {
			logrus.Info("Install java agent")
			response, newPort := Prepare(processName, processId)
			if !response.Success {
				return response
			}
			port = newPort
			delete(model.ActionFlags,"override")
		}
	}

	var result string
	var code int
	if isDestroy {
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
		result, err, code = util.PostCurl(url_, body, "")
	}
	if err != nil {
		return spec.ReturnFail(spec.Code[spec.SandboxInvokeError], err.Error())
	}
	if code == 404 {
		return spec.ReturnFail(spec.Code[spec.JavaAgentCmdError], "please restart application to inject fault")
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
	record, err := e.getRecordFromDB(process, "")
	if err != nil {
		return spec.ReturnFail(spec.Code[spec.SandboxInvokeError], err.Error())
	}
	if record == nil {
		return spec.ReturnFail(spec.Code[spec.DatabaseError], fmt.Sprintf("record not found, uid: %s", uid))
	}
	port := record.Port
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

func (e *Executor) getRecordFromDB(processName, processId string) (*data.PreparationRecord, error) {
	if processName != "" || processId != "" {
		pid, response := CheckFlagValues(processName, processId)
		if !response.Success {
			return nil, fmt.Errorf(response.Err)
		}
		processId = pid
	}
	record, err := db.QueryRunningPreByTypeAndProcess("jvm", processName, processId)
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
func CheckFlagValues(processName, processId string) (string, *spec.Response) {
	cl := channel.NewLocalChannel()
	if processName == "" {
		if processId == "" {
			return "", spec.ReturnFail(spec.Code[spec.GetProcessError], fmt.Sprintf("cant get the process id"))
		}
		exists, err := cl.ProcessExists(processId)
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
		ctx := context.WithValue(context.Background(), channel.ProcessKey, "java")
		// set pecchannel.ExcludeProcessKey as "blade" to exclude pid of the blade command we run when querying the target application by processName
		// If ExcludeProcessKey is not set, multiple pids might be returned (the blade command pid might be one of the pids.)
		ctx = context.WithValue(ctx, channel.ExcludeProcessKey, "blade")
		pids, err := cl.GetPidsByProcessName(processName, ctx)
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

func Prepare(processName, processId string) (response *spec.Response, port string) {
	processId, response = CheckFlagValues(processName, processId)
	if !response.Success {
		return
	}
	record, err := db.QueryRunningPreByTypeAndProcess("jvm", processName, processId)
	if record == nil || err != nil || record.Uid == "" {
		// get port from local port
		port, err = getAndCacheSandboxPort()
		if err != nil {
			return spec.ReturnFail(spec.Code[spec.ServerError],
				fmt.Sprintf("get sandbox port err, %s", err.Error())), port
		}
		record, err = insertPrepareRecord("jvm", processName, port, processId)
		if err != nil {
			return spec.ReturnFail(spec.Code[spec.DatabaseError],
				fmt.Sprintf("insert prepare record err, %s", err.Error())), port
		}
	}
	var username string
	port = record.Port
	response, username = Attach(port, "", processId)
	if !response.Success && username != "" && strings.Contains(response.Err, "connection refused") {
		// if attach failed, search port from ~/.sandbox.token
		port, err = CheckPortFromSandboxToken(username)
		if err == nil {
			logrus.Infof("use %s port to retry", port)
			response, username = Attach(port, "", processId)
			if response.Success {
				// update port
				err := db.UpdatePreparationPortByUid(record.Uid, port)
				if err != nil {
					logrus.Warningf("update preparation port failed, %v", err)
				}
			}
		}
	}
	if record.Pid != processId {
		// update pid
		db.UpdatePreparationPortByUid(record.Uid, processId)
	}
	handlePrepareResponse(record.Uid, response)
	return response, port
}

// Revoke 卸载 Java agent
func Revoke(record *data.PreparationRecord, processName, processId string) *spec.Response {
	var port string
	if record == nil {
		processId, response := CheckFlagValues(processName, processId)
		if !response.Success {
			return response
		}
		username, err := getUsername(processId)
		if err != nil {
			return spec.ReturnFail(spec.Code[spec.StatusError],
				fmt.Sprintf("get username failed by %s pid, %v", username, err))
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
	if response := Detach(port); !response.Success {
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
	err = db.InsertPreparationRecord(record)
	if err != nil {
		return nil, err
	}
	return record, nil
}

func handlePrepareResponse(uid string, response *spec.Response) {
	response.Result = uid
	if !response.Success {
		db.UpdatePreparationRecordByUid(uid, "Error", response.Err)
		return
	}
	err := db.UpdatePreparationRecordByUid(uid, "Running", "")
	if err != nil {
		logrus.Warningf("update preparation record error: %s", err.Error())
	}
}
