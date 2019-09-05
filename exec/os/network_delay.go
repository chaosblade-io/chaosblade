package os

import (
	"github.com/chaosblade-io/chaosblade/exec"
	"github.com/chaosblade-io/chaosblade/transport"
	"context"
	"path"
	"fmt"
	"github.com/chaosblade-io/chaosblade/util"
)

type DelayActionSpec struct {
}

func (*DelayActionSpec) Name() string {
	return "delay"
}

func (*DelayActionSpec) Aliases() []string {
	return []string{}
}

func (*DelayActionSpec) ShortDesc() string {
	return "Delay experiment"
}

func (*DelayActionSpec) LongDesc() string {
	return "Delay experiment"
}

func (*DelayActionSpec) Matchers() []exec.ExpFlagSpec {
	return commFlags
}

func (*DelayActionSpec) Flags() []exec.ExpFlagSpec {
	return []exec.ExpFlagSpec{
		&exec.ExpFlag{
			Name:     "time",
			Desc:     "Delay time, ms",
			Required: true,
		},
		&exec.ExpFlag{
			Name: "offset",
			Desc: "Delay offset time, ms",
		},
	}
}

type NetworkDelayExecutor struct {
	channel exec.Channel
}

func (de *NetworkDelayExecutor) Name() string {
	return "delay"
}

func (de *NetworkDelayExecutor) Exec(uid string, ctx context.Context, model *exec.ExpModel) *transport.Response {
	if de.channel == nil {
		return transport.ReturnFail(transport.Code[transport.ServerError], "channel is nil")
	}
	netInterface := model.ActionFlags["interface"]
	if netInterface == "" {
		return transport.ReturnFail(transport.Code[transport.IllegalParameters], "less interface parameter")
	}
	time := model.ActionFlags["time"]
	if time == "" {
		return transport.ReturnFail(transport.Code[transport.IllegalParameters], "less time flag")
	}
	offset := model.ActionFlags["offset"]
	if offset == "" {
		offset = "10"
	}
	localPort := model.ActionFlags["local-port"]
	remotePort := model.ActionFlags["remote-port"]
	excludePort := model.ActionFlags["exclude-port"]
	destIp := model.ActionFlags["destination-ip"]
	if _, ok := exec.IsDestroy(ctx); ok {
		return de.stop(netInterface, ctx)
	} else {
		return de.start(localPort, remotePort, excludePort, destIp, time, offset, netInterface, ctx)
	}
}

func (de *NetworkDelayExecutor) start(localPort, remotePort, excludePort, destIp, time, offset, netInterface string,
	ctx context.Context) *transport.Response {
	args := fmt.Sprintf("--start --interface %s --time %s --offset %s --debug=%t", netInterface, time, offset, util.Debug)
	args, err := getCommArgs(localPort, remotePort, excludePort, destIp, args)
	if err != nil {
		return transport.ReturnFail(transport.Code[transport.IllegalParameters], err.Error())
	}
	return de.channel.Run(ctx, path.Join(de.channel.GetScriptPath(), dlNetworkBin), args)
}

func (de *NetworkDelayExecutor) stop(netInterface string, ctx context.Context) *transport.Response {
	return de.channel.Run(ctx, path.Join(de.channel.GetScriptPath(), dlNetworkBin),
		fmt.Sprintf("--stop --interface %s --debug=%t", netInterface, util.Debug))
}

func (de *NetworkDelayExecutor) SetChannel(channel exec.Channel) {
	de.channel = channel
}

func (*DelayActionSpec) Executor(channel exec.Channel) exec.Executor {
	return &NetworkDelayExecutor{channel}
}
