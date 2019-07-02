package jvm

import (
	"context"
	"encoding/json"
	"fmt"
  
	"github.com/chaosblade-io/chaosblade/data"
	"github.com/chaosblade-io/chaosblade/exec"
	"github.com/chaosblade-io/chaosblade/transport"
	"github.com/chaosblade-io/chaosblade/util"
	neturl "net/url"
	"strings"
)

const DefaultUri = "sandbox/default/module/http/chaosblade"

// Executor for jvm experiment
type Executor struct {
	Uri     string
	channel exec.Channel
}

func NewExecutor() *Executor {
	return &Executor{
		Uri:     DefaultUri,
		channel: exec.NewLocalChannel(),
	}
}

func (e *Executor) Name() string {
	return "jvm"
}

func (e *Executor) SetChannel(channel exec.Channel) {
	e.channel = channel
}

func (e *Executor) Exec(uid string, ctx context.Context, model *exec.ExpModel) *transport.Response {
	var url_ string
	port, err := e.getPortFromDB(model.ActionFlags["process"])
	if err != nil {
		return transport.ReturnFail(transport.Code[transport.ServerError], "cannot get port from local")
	}
	if _, ok := exec.IsDestroy(ctx); ok {
		url_ = e.destroyUrl(port, uid)
	} else {
		url_ = e.createUrl(port, uid, model)
	}
	result, err, code := util.Curl(url_)
	if err != nil {
		return transport.ReturnFail(transport.Code[transport.SandboxInvokeError], err.Error())
	}
	if code == 404 {
		return transport.ReturnFail(transport.Code[transport.JavaAgentCmdError], "please invoke attach command first")
	}
	var resp transport.Response
	json.Unmarshal([]byte(result), &resp)
	return &resp
}

func (e *Executor) createUrl(port, suid string, model *exec.ExpModel) string {
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

func (e *Executor) QueryStatus(uid string) *transport.Response {
	experimentModel, err := db.QueryExperimentModelByUid(uid)
	if err != nil {
		return transport.ReturnFail(transport.Code[transport.DatabaseError],
			fmt.Sprintf("query experiment error, %s", err.Error()))
	}
	if experimentModel == nil {
		return transport.ReturnFail(transport.Code[transport.DataNotFound], "the experiment record not found")
	}
	// get process flag
	process := getProcessFlagFromExpRecord(experimentModel)
	port, err := e.getPortFromDB(process)
	if err != nil {
		return transport.ReturnFail(transport.Code[transport.SandboxInvokeError], err.Error())
	}
	url := e.statusUrl(port, uid)
	result, err, code := util.Curl(url)
	if err != nil {
		return transport.ReturnFail(transport.Code[transport.SandboxInvokeError], err.Error())
	}
	if code == 404 {
		return transport.ReturnFail(transport.Code[transport.SandboxInvokeError], "the command not support")
	}
	if code != 200 {
		return transport.ReturnFail(transport.Code[transport.SandboxInvokeError], result)
	}
	var resp transport.Response
	json.Unmarshal([]byte(result), &resp)
	return &resp
}

func (e *Executor) statusUrl(port, uid string) string {
	return fmt.Sprintf("http://%s:%s/%s/status?suid=%s", "127.0.0.1", port, e.Uri, uid)
}

var db = data.GetSource()

func (e *Executor) getPortFromDB(process string) (string, error) {
	record, err := db.QueryRunningPreByTypeAndProcess("jvm", process)
	if err != nil {
		return "", err
	}
	if record == nil {
		return "", fmt.Errorf("port not found for %s process, please execute prepare command firstly", process)
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
