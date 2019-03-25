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
			Name: "service-port",
			Desc: "Port for external service",
		},
		&exec.ExpFlag{
			Name:     "device",
			Desc:     "Network device",
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
	if device, ok := model.ActionFlags["device"]; ok {
		if device == "" {
			return transport.ReturnFail(transport.Code[transport.IllegalParameters], "less device parameter")
		}
		dev = device
	}
	percent := model.ActionFlags["percent"]
	if percent == "" {
		return transport.ReturnFail(transport.Code[transport.IllegalParameters], "less percent flag")
	}
	if _, ok := exec.IsDestroy(ctx); ok {
		return nle.stop(dev, ctx)
	} else {
		servicePort := model.ActionFlags["service-port"]
		return nle.start(dev, servicePort, percent, ctx)
	}
}

func (nle *NetworkLossExecutor) start(device, servicePort, percent string, ctx context.Context) *transport.Response {
	args := fmt.Sprintf("--start --device %s --percent %s", device, percent)
	if servicePort != "" {
		args = fmt.Sprintf("%s --service-port %s", args, servicePort)
	}
	return nle.channel.Run(ctx, path.Join(nle.channel.GetScriptPath(), lossNetworkBin), args)
}

func (nle *NetworkLossExecutor) stop(device string, ctx context.Context) *transport.Response {
	return nle.channel.Run(ctx, path.Join(nle.channel.GetScriptPath(), lossNetworkBin),
		fmt.Sprintf("--stop --device %s", device))
}

func (nle *NetworkLossExecutor) SetChannel(channel exec.Channel) {
	nle.channel = channel
}

func (*LossActionSpec) Executor(channel exec.Channel) exec.Executor {
	return &NetworkLossExecutor{channel: channel}
}
