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
	return "Fill the specified directory path or mount point"
}

func (*FillActionSpec) LongDesc() string {
	return "Fill the specified directory path or mount point. If the path is not directory or does not exist, an error message will be returned. Only one can be selected in --path and --mount-point"
}

func (*FillActionSpec) Flags() []exec.ExpFlagSpec {
	return []exec.ExpFlagSpec{
		&exec.ExpFlag{
			Name:     "size",
			Desc:     "Disk fill size, unit is MB. The value is a positive integer without unit, for example, --size 1024",
			Required: true,
		},
		&exec.ExpFlag{
			Name: "path",
			Desc: "The path of directory where the disk is populated",
		},
		// Mount-point flag in parent cannot be deleted because it needs to be adapted to the old version
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
	path := model.ActionFlags["path"]
	if mountPoint == "" && path == "" {
		return transport.ReturnFail(transport.Code[transport.IllegalParameters],
			"must use --path or --mount-point to specify the directory")
	}
	if mountPoint != "" && path != "" {
		return transport.ReturnFail(transport.Code[transport.IllegalParameters],
			"only one can be select in --path and --mount-point")
	}
	directory := path
	if directory == "" {
		directory = mountPoint
	}
	if !util.IsDir(directory) {
		return transport.ReturnFail(transport.Code[transport.IllegalParameters],
			fmt.Sprintf("the %s directory does not exist or is not directory", directory))
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
		return fae.stop(directory, size, ctx)
	} else {
		return fae.start(directory, size, ctx)
	}
}

func (fae *FillActionExecutor) start(directory, size string, ctx context.Context) *transport.Response {
	return fae.channel.Run(ctx, path.Join(fae.channel.GetScriptPath(), fillDiskBin),
		fmt.Sprintf("--directory %s --size %s --start", directory, size))
}

func (fae *FillActionExecutor) stop(directory, size string, ctx context.Context) *transport.Response {
	return fae.channel.Run(ctx, path.Join(fae.channel.GetScriptPath(), fillDiskBin),
		fmt.Sprintf("--directory %s --stop", directory))
}

func (fae *FillActionExecutor) SetChannel(channel exec.Channel) {
	fae.channel = channel
}

func (*FillActionSpec) Executor(channel exec.Channel) exec.Executor {
	return &FillActionExecutor{channel: channel}
}
