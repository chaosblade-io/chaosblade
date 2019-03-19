package os

import (
	"github.com/chaosblade-io/chaosblade/exec"
	"github.com/chaosblade-io/chaosblade/transport"
	"context"
	"fmt"
	"path"
)

type FillActionSpec struct {
}

func (*FillActionSpec) Name() string {
	return "fill"
}

func (*FillActionSpec) Aliases() []string {
	return []string{}
}

func (*FillActionSpec) ShortDesc() string {
	return "Fill the mounted disk"
}

func (*FillActionSpec) LongDesc() string {
	return "Fill the mounted disk"
}

func (*FillActionSpec) Flags() []exec.ExpFlagSpec {
	return []exec.ExpFlagSpec{
		&exec.ExpFlag{
			Name:     "size",
			Desc:     "fill size, MB",
			Required: true,
		},
	}
}

func (*FillActionSpec) Matchers() []exec.ExpFlagSpec {
	return []exec.ExpFlagSpec{}
}

type FillActionExecutor struct {
	channel exec.Channel
}

func (*FillActionExecutor) Name() string {
	return "fill"
}

var fillDiskBin = "chaos_filldisk"

func (fae *FillActionExecutor) Exec(uid string, ctx context.Context, model *exec.ExpModel) *transport.Response {
	if fae.channel == nil {
		return transport.ReturnFail(transport.Code[transport.ServerError], "channel is nil")
	}
	device := model.ActionFlags["mount-on"]
	if device == "" {
		device = "/"
	}
	size := model.ActionFlags["size"]
	if size == "" {
		return transport.ReturnFail(transport.Code[transport.IllegalParameters], "less size arg")
	}
	if _, ok := exec.IsDestroy(ctx); ok {
		return fae.stop(device, size, ctx)
	} else {
		return fae.start(device, size, ctx)
	}
}

func (fae *FillActionExecutor) start(device, size string, ctx context.Context) *transport.Response {
	return fae.channel.Run(ctx, path.Join(fae.channel.GetScriptPath(), fillDiskBin),
		fmt.Sprintf("--device %s --size %s --start", device, size))
}

func (fae *FillActionExecutor) stop(device, size string, ctx context.Context) *transport.Response {
	return fae.channel.Run(ctx, path.Join(fae.channel.GetScriptPath(), fillDiskBin),
		fmt.Sprintf("--device %s --stop", device))
}

func (fae *FillActionExecutor) SetChannel(channel exec.Channel) {
	fae.channel = channel
}

func (*FillActionSpec) Executor(channel exec.Channel) exec.Executor {
	return &FillActionExecutor{channel: channel}
}
