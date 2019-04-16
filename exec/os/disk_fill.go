package os

import (
	"github.com/chaosblade-io/chaosblade/exec"
	"github.com/chaosblade-io/chaosblade/transport"
	"context"
	"fmt"
	"path"
	"github.com/chaosblade-io/chaosblade/util"
	"strconv"
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
			Desc:     "fill size, unit is MB",
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
	mountPoint := model.ActionFlags["mount-point"]
	if mountPoint == "" {
		mountPoint = "/"
	}
	if !util.IsExist(mountPoint) {
		return transport.ReturnFail(transport.Code[transport.IllegalParameters],
			fmt.Sprintf("the %s mount point is not exist", mountPoint))
	}
	size := model.ActionFlags["size"]
	if size == "" {
		return transport.ReturnFail(transport.Code[transport.IllegalParameters], "less size arg")
	}
	_, err := strconv.Atoi(size)
	if err != nil {
		return transport.ReturnFail(transport.Code[transport.IllegalParameters], "size must be positive integer")
	}
	if _, ok := exec.IsDestroy(ctx); ok {
		return fae.stop(mountPoint, size, ctx)
	} else {
		return fae.start(mountPoint, size, ctx)
	}
}

func (fae *FillActionExecutor) start(mountPoint, size string, ctx context.Context) *transport.Response {
	return fae.channel.Run(ctx, path.Join(fae.channel.GetScriptPath(), fillDiskBin),
		fmt.Sprintf("--mount-point %s --size %s --start", mountPoint, size))
}

func (fae *FillActionExecutor) stop(mountPoint, size string, ctx context.Context) *transport.Response {
	return fae.channel.Run(ctx, path.Join(fae.channel.GetScriptPath(), fillDiskBin),
		fmt.Sprintf("--mount-point %s --stop", mountPoint))
}

func (fae *FillActionExecutor) SetChannel(channel exec.Channel) {
	fae.channel = channel
}

func (*FillActionSpec) Executor(channel exec.Channel) exec.Executor {
	return &FillActionExecutor{channel: channel}
}
