package os

import (
	"github.com/chaosblade-io/chaosblade/exec"
	"github.com/chaosblade-io/chaosblade/transport"
	"context"
	"path"
	"fmt"
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
			Name:     "device",
			Desc:     "Network device",
			Required: true,
		},
	}
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
	device := model.ActionFlags["device"]
	if device == "" {
		return transport.ReturnFail(transport.Code[transport.IllegalParameters], "less device parameter")
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
	if _, ok := exec.IsDestroy(ctx); ok {
		return de.stop(device, ctx)
	} else {
		return de.start(localPort, remotePort, excludePort, time, offset, device, ctx)
	}
}

var delayNetworkBin = "chaos_delaynetwork"

func (de *NetworkDelayExecutor) start(localPort, remotePort, excludePort, time, offset, device string, ctx context.Context) *transport.Response {
	args := fmt.Sprintf("--start --device %s --time %s --offset %s", device, time, offset)
	if localPort != "" {
		args = fmt.Sprintf("%s --local-port %s", args, localPort)
	}
	if remotePort != "" {
		args = fmt.Sprintf("%s --remote-port %s", args, remotePort)
	}
	if excludePort != "" {
		args = fmt.Sprintf("%s --exclude-port %s", args, excludePort)
	}
	return de.channel.Run(ctx, path.Join(de.channel.GetScriptPath(), delayNetworkBin), args)
}

func (de *NetworkDelayExecutor) stop(device string, ctx context.Context) *transport.Response {
	return de.channel.Run(ctx, path.Join(de.channel.GetScriptPath(), delayNetworkBin),
		fmt.Sprintf("--stop --device %s", device))
}

func (de *NetworkDelayExecutor) SetChannel(channel exec.Channel) {
	de.channel = channel
}

func (*DelayActionSpec) Executor(channel exec.Channel) exec.Executor {
	return &NetworkDelayExecutor{channel}
}
