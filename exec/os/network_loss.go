package os

import (
	"github.com/chaosblade-io/chaosblade/exec"
	"github.com/chaosblade-io/chaosblade/transport"
	"context"
	"path"
	"fmt"
)

type LossActionSpec struct {
}

func (*LossActionSpec) Name() string {
	return "loss"
}

func (*LossActionSpec) Aliases() []string {
	return []string{}
}

func (*LossActionSpec) ShortDesc() string {
	return "Loss network package"
}

func (*LossActionSpec) LongDesc() string {
	return "Loss network package"
}

func (*LossActionSpec) Matchers() []exec.ExpFlagSpec {
	return []exec.ExpFlagSpec{
		&exec.ExpFlag{
			Name: "local-port",
			Desc: "Port for local service",
		},
		&exec.ExpFlag{
			Name: "remote-port",
			Desc: "Port for remote service",
		},
		&exec.ExpFlag{
			Name: "exclude-port",
			Desc: "Exclude one local port, for example 22 port. This flag is invalid when --local-port or --remote-port is specified",
		},
		&exec.ExpFlag{
			Name:     "interface",
			Desc:     "Network interface, for example, eth0",
			Required: true,
		},
	}
}

func (*LossActionSpec) Flags() []exec.ExpFlagSpec {
	return []exec.ExpFlagSpec{
		&exec.ExpFlag{
			Name:     "percent",
			Desc:     "loss percent, [0, 100]",
			Required: true,
		},
	}
}

type NetworkLossExecutor struct {
	channel exec.Channel
}

func (*NetworkLossExecutor) Name() string {
	return "loss"
}

var lossNetworkBin = "chaos_lossnetwork"

func (nle *NetworkLossExecutor) Exec(uid string, ctx context.Context, model *exec.ExpModel) *transport.Response {
	if nle.channel == nil {
		return transport.ReturnFail(transport.Code[transport.ServerError], "channel is nil")
	}
	var dev = ""
	if netInterface, ok := model.ActionFlags["interface"]; ok {
		if netInterface == "" {
			return transport.ReturnFail(transport.Code[transport.IllegalParameters], "less interface flag")
		}
		dev = netInterface
	}
	percent := model.ActionFlags["percent"]
	if percent == "" {
		return transport.ReturnFail(transport.Code[transport.IllegalParameters], "less percent flag")
	}
	if _, ok := exec.IsDestroy(ctx); ok {
		return nle.stop(dev, ctx)
	} else {
		localPort := model.ActionFlags["local-port"]
		remotePort := model.ActionFlags["remote-port"]
		excludePort := model.ActionFlags["exclude-port"]
		return nle.start(dev, localPort, remotePort, excludePort, percent, ctx)
	}
}

func (nle *NetworkLossExecutor) start(netInterface, localPort, remotePort, excludePort, percent string, ctx context.Context) *transport.Response {
	args := fmt.Sprintf("--start --interface %s --percent %s", netInterface, percent)
	if localPort != "" {
		args = fmt.Sprintf("%s --local-port %s", args, localPort)
	}
	if remotePort != "" {
		args = fmt.Sprintf("%s --remote-port %s", args, remotePort)
	}
	if excludePort != "" {
		args = fmt.Sprintf("%s --exclude-port %s", args, excludePort)
	}
	return nle.channel.Run(ctx, path.Join(nle.channel.GetScriptPath(), lossNetworkBin), args)
}

func (nle *NetworkLossExecutor) stop(netInterface string, ctx context.Context) *transport.Response {
	return nle.channel.Run(ctx, path.Join(nle.channel.GetScriptPath(), lossNetworkBin),
		fmt.Sprintf("--stop --interface %s", netInterface))
}

func (nle *NetworkLossExecutor) SetChannel(channel exec.Channel) {
	nle.channel = channel
}

func (*LossActionSpec) Executor(channel exec.Channel) exec.Executor {
	return &NetworkLossExecutor{channel: channel}
}
