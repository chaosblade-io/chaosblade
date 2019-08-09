package cplus

import (
	"github.com/chaosblade-io/chaosblade/exec"
	"context"
	"github.com/chaosblade-io/chaosblade/transport"
	"github.com/chaosblade-io/chaosblade/util"
	"encoding/json"
	"fmt"
	"github.com/chaosblade-io/chaosblade/data"
	neturl "net/url"
)

// Executor for jvm experiment
type Executor struct {
	Uri     string
	channel exec.Channel
}

func NewExecutor() *Executor {
	return &Executor{
		channel: exec.NewLocalChannel(),
	}
}

func (e *Executor) Name() string {
	return "cplus"
}

func (e *Executor) SetChannel(channel exec.Channel) {
	e.channel = channel
}

func (e *Executor) Exec(uid string, ctx context.Context, model *exec.ExpModel) *transport.Response {
	var url string
	port, err := e.getPortFromDB(model)
	if err != nil {
		return transport.ReturnFail(transport.Code[transport.ServerError], "cannot get port from local")
	}
	if _, ok := exec.IsDestroy(ctx); ok {
		url = e.destroyUrl(port, uid)
	} else {
		url = e.createUrl(port, uid, model)
	}
	result, err, _ := util.Curl(url)
	if err != nil {
		return transport.ReturnFail(transport.Code[transport.CplusProxyCmdError], err.Error())
	}
	var resp transport.Response
	json.Unmarshal([]byte(result), &resp)
	return &resp
}

func (e *Executor) createUrl(port, suid string, model *exec.ExpModel) string {
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

func (e *Executor) getPortFromDB(model *exec.ExpModel) (string, error) {
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
