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
			Name: "service-port",
			Desc: "Port for external service",
		},
		&exec.ExpFlag{
			Name: "invoke-port",
			Desc: "Port for invoke",
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
	servicePort := model.ActionFlags["service-port"]
	invokePort := model.ActionFlags["invoke-port"]
	if _, ok := exec.IsDestroy(ctx); ok {
		return de.stop(device, ctx)
	} else {
		return de.start(servicePort, invokePort, time, offset, device, ctx)
	}
}

var delayNetworkBin = "chaos_delaynetwork"

func (de *NetworkDelayExecutor) start(servicePort, invokePort, time, offset, device string, ctx context.Context) *transport.Response {
	args := fmt.Sprintf("--start --device %s --time %s --offset %s", device, time, offset)
	if servicePort != "" {
		args = fmt.Sprintf("%s --service-port %s", args, servicePort)
	}
	if invokePort != "" {
		args = fmt.Sprintf("%s --invoke-port %s", args, invokePort)
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
