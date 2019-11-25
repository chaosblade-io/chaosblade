package os

import (
	"context"
	"fmt"

	"github.com/chaosblade-io/chaosblade-exec-os/exec/model"
	"github.com/chaosblade-io/chaosblade-spec-go/channel"
	"github.com/chaosblade-io/chaosblade-spec-go/spec"
)

type Executor struct {
	executors map[string]spec.Executor
}

func NewExecutor() spec.Executor {
	return &Executor{
		executors: model.GetAllOsExecutors(),
	}
}

func (*Executor) Name() string {
	return "os"
}

func (e *Executor) Exec(uid string, ctx context.Context, model *spec.ExpModel) *spec.Response {
	key := model.Target + model.ActionName
	executor := e.executors[key]
	if executor == nil {
		return spec.ReturnFail(spec.Code[spec.HandlerNotFound], fmt.Sprintf("the os executor not found, %s", key))
	}
	executor.SetChannel(channel.NewLocalChannel())
	return executor.Exec(uid, ctx, model)
}

func (*Executor) SetChannel(channel spec.Channel) {
}
