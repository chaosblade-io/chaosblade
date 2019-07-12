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
			Name:     "count",
			Desc:     "fill disk block count, unit is MB",
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
	count := model.ActionFlags["count"]
	if count == "" {
		return transport.ReturnFail(transport.Code[transport.IllegalParameters], "less count arg")
	}
	_, err := strconv.Atoi(count)
	if err != nil {
		return transport.ReturnFail(transport.Code[transport.IllegalParameters], "count must be positive integer")
	}
	if _, ok := exec.IsDestroy(ctx); ok {
		return fae.stop(mountPoint, count, ctx)
	}
	return fae.start(mountPoint, count, ctx)
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
