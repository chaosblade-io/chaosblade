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
const Sandbox404 = "Error 404 Not Found"

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
	port, err := e.getPortFromDB(model)
	if err != nil {
		return transport.ReturnFail(transport.Code[transport.ServerError], "cannot get port from local")
	}
	if _, ok := exec.IsDestroy(ctx); ok {
		url_ = e.destroyUrl(port, uid)
	} else {
		url_ = e.createUrl(port, uid, model)
	}
	result, err := util.Curl(url_)
	if err != nil {
		return transport.ReturnFail(transport.Code[transport.SandboxInvokeError], err.Error())
	}
	if strings.Contains(result, Sandbox404) {
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

var db = data.GetSource()

func (e *Executor) getPortFromDB(model *exec.ExpModel) (string, error) {
	processName := model.ActionFlags["process"]
	record, err := db.QueryRunningPreByTypeAndProcess("jvm", processName)
	if err != nil {
		return "", err
	}
	if record == nil {
		return "", fmt.Errorf("port not found for %s process, please execute prepare command firstly", processName)
	}
	return record.Port, nil
}
