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
			Name: "local-port",
			Desc: "Port for local service",
		},
		&exec.ExpFlag{
			Name: "remote-port",
			Desc: "Port for remote service",
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
	localPort := model.ActionFlags["local-port"]
	remotePort := model.ActionFlags["remote-port"]
	if _, ok := exec.IsDestroy(ctx); ok {
		return ne.stop(localPort, remotePort, ctx)
	} else {
		return ne.start(localPort, remotePort, ctx)
	}
}

func (ne *NetworkDropExecutor) start(localPort, remotePort string, ctx context.Context) *transport.Response {
	args := "--start"
	if localPort != "" {
		args = fmt.Sprintf("%s --local-port %s", args, localPort)
	}
	if remotePort != "" {
		args = fmt.Sprintf("%s --remote-port %s", args, remotePort)
	}
	return ne.channel.Run(ctx, path.Join(ne.channel.GetScriptPath(), dropNetworkBin), args)
}

func (ne *NetworkDropExecutor) stop(localPort, remotePort string, ctx context.Context) *transport.Response {
	args := "--stop"
	if localPort != "" {
		args = fmt.Sprintf("%s --local-port %s", args, localPort)
	}
	if remotePort != "" {
		args = fmt.Sprintf("%s --remote-port %s", args, remotePort)
	}
	return ne.channel.Run(ctx, path.Join(ne.channel.GetScriptPath(), dropNetworkBin), args)
}

func (ne *NetworkDropExecutor) SetChannel(channel exec.Channel) {
	ne.channel = channel
}

func (*DropActionSpec) Executor(channel exec.Channel) exec.Executor {
	return &NetworkDropExecutor{channel}
}
