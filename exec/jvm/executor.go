package jvm

import (
	"context"
	"encoding/json"
	"fmt"

	"github.com/chaosblade-io/chaosblade/data"
	"github.com/chaosblade-io/chaosblade-spec-go/spec"
	"github.com/chaosblade-io/chaosblade-spec-go/util"
	neturl "net/url"
	"strings"
	specchannel "github.com/chaosblade-io/chaosblade-spec-go/channel"
)

const DefaultUri = "sandbox/default/module/http/chaosblade"

// Executor for jvm experiment
type Executor struct {
	Uri     string
	channel spec.Channel
}

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
	if _, ok := spec.IsDestroy(ctx); ok {
		url_ = e.destroyUrl(port, uid)
	} else {
		url_ = e.createUrl(port, uid, model)
	}
	result, err, code := util.Curl(url_)
	if err != nil {
		return spec.ReturnFail(spec.Code[spec.SandboxInvokeError], err.Error())
	}
	if code == 404 {
		return spec.ReturnFail(spec.Code[spec.JavaAgentCmdError], "please execute prepare command first")
	}
	var resp spec.Response
	json.Unmarshal([]byte(result), &resp)
	return &resp
}

func (e *Executor) createUrl(port, suid string, model *spec.ExpModel) string {
	url := fmt.Sprintf("http://%s:%s/%s/create?target=%s&suid=%s&action=%s",
		"127.0.0.1", port, e.Uri, model.Target, suid, model.ActionName)
	for k, v := range model.ActionFlags {
		if v == "" || v == "false" {
			continue
		}
		// filter timeout because of the java agent implementation by all matchers
		if k == "timeout" {
			continue
		}
		url = fmt.Sprintf("%s&%s=%s", url, k, neturl.QueryEscape(v))
	}
	return url
}

func (e *Executor) destroyUrl(port, uid string) string {
	url := fmt.Sprintf("http://%s:%s/%s/destroy?suid=%s",
		"127.0.0.1", port, e.Uri, uid)
	return url
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
	url := e.statusUrl(port, uid)
	result, err, code := util.Curl(url)
	if err != nil {
		return spec.ReturnFail(spec.Code[spec.SandboxInvokeError], err.Error())
	}
	if code == 404 {
		return spec.ReturnFail(spec.Code[spec.SandboxInvokeError], "the command not support")
	}
	if code != 200 {
		return spec.ReturnFail(spec.Code[spec.SandboxInvokeError], result)
	}
	var resp spec.Response
	json.Unmarshal([]byte(result), &resp)
	return &resp
}

func (e *Executor) statusUrl(port, uid string) string {
	return fmt.Sprintf("http://%s:%s/%s/status?suid=%s", "127.0.0.1", port, e.Uri, uid)
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
