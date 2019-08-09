package kubernetes

import (
	"github.com/chaosblade-io/chaosblade/transport"
	"github.com/chaosblade-io/chaosblade/exec"
	"context"
)

type Executor struct {
}

func (*Executor) Name() string {
	return "k8s"
}

func (e *Executor) SetChannel(channel exec.Channel) {
}

func (*Executor) Exec(uid string, ctx context.Context, model *exec.ExpModel) *transport.Response {
	return transport.ReturnSuccess("k8s command")
}
