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
	return commFlags
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
	}
	localPort := model.ActionFlags["local-port"]
	remotePort := model.ActionFlags["remote-port"]
	excludePort := model.ActionFlags["exclude-port"]
	return nle.start(dev, localPort, remotePort, excludePort, percent, ctx)
}

func (nle *NetworkLossExecutor) start(netInterface, localPort, remotePort, excludePort, percent string, ctx context.Context) *transport.Response {
	args := fmt.Sprintf("--start --interface %s --percent %s", netInterface, percent)
	args, err := getCommArgs(localPort, remotePort, excludePort, args)
	if err != nil {
		return transport.ReturnFail(transport.Code[transport.IllegalParameters], err.Error())
	}
	return nle.channel.Run(ctx, path.Join(nle.channel.GetScriptPath(), dlNetworkBin), args)
}

func (nle *NetworkLossExecutor) stop(netInterface string, ctx context.Context) *transport.Response {
	return nle.channel.Run(ctx, path.Join(nle.channel.GetScriptPath(), dlNetworkBin),
		fmt.Sprintf("--stop --interface %s", netInterface))
}

func (nle *NetworkLossExecutor) SetChannel(channel exec.Channel) {
	nle.channel = channel
}

func (*LossActionSpec) Executor(channel exec.Channel) exec.Executor {
	return &NetworkLossExecutor{channel: channel}
}
