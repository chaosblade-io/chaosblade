package os

import (
	"github.com/chaosblade-io/chaosblade/exec"
	"github.com/chaosblade-io/chaosblade/transport"
	"context"
	"path"
	"fmt"
)

type DropActionSpec struct {
}

func (*DropActionSpec) Name() string {
	return "drop"
}

func (*DropActionSpec) Aliases() []string {
	return []string{}
}

func (*DropActionSpec) ShortDesc() string {
	return "Drop experiment"
}

func (*DropActionSpec) LongDesc() string {
	return "Drop network data"
}

func (*DropActionSpec) Matchers() []exec.ExpFlagSpec {
	return []exec.ExpFlagSpec{
		&exec.ExpFlag{
			Name: "service-port",
			Desc: "Port for external service",
		},
		&exec.ExpFlag{
			Name: "invoke-port",
			Desc: "Port for invoking",
		},
	}
}

func (*DropActionSpec) Flags() []exec.ExpFlagSpec {
	return []exec.ExpFlagSpec{}
}

type NetworkDropExecutor struct {
	channel exec.Channel
}

func (*NetworkDropExecutor) Name() string {
	return "drop"
}

var dropNetworkBin = "chaos_dropnetwork"

func (ne *NetworkDropExecutor) Exec(suid string, ctx context.Context, model *exec.ExpModel) *transport.Response {
	if ne.channel == nil {
		return transport.ReturnFail(transport.Code[transport.ServerError], "channel is nil")
	}
	servicePort := model.ActionFlags["service-port"]
	invokePort := model.ActionFlags["invoke-port"]
	if _, ok := exec.IsDestroy(ctx); ok {
		return ne.stop(servicePort, invokePort, ctx)
	} else {
		return ne.start(servicePort, invokePort, ctx)
	}
}

func (ne *NetworkDropExecutor) start(servicePort, invokePort string, ctx context.Context) *transport.Response {
	args := "--start"
	if servicePort != "" {
		args = fmt.Sprintf("%s --service-port %s", args, servicePort)
	}
	if invokePort != "" {
		args = fmt.Sprintf("%s --invoke-port %s", args, invokePort)
	}
	return ne.channel.Run(ctx, path.Join(ne.channel.GetScriptPath(), dropNetworkBin), args)
}

func (ne *NetworkDropExecutor) stop(servicePort, invokePort string, ctx context.Context) *transport.Response {
	args := "--stop"
	if servicePort != "" {
		args = fmt.Sprintf("%s --service-port %s", args, servicePort)
	}
	if invokePort != "" {
		args = fmt.Sprintf("%s --invoke-port %s", args, invokePort)
	}
	return ne.channel.Run(ctx, path.Join(ne.channel.GetScriptPath(), dropNetworkBin), args)
}

func (ne *NetworkDropExecutor) SetChannel(channel exec.Channel) {
	ne.channel = channel
}

func (*DropActionSpec) Executor(channel exec.Channel) exec.Executor {
	return &NetworkDropExecutor{channel}
}
