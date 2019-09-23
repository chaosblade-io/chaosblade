package os

import (
	"github.com/chaosblade-io/chaosblade/exec"
	"github.com/chaosblade-io/chaosblade/transport"
	"context"
	"path"
	"fmt"
	"github.com/chaosblade-io/chaosblade/util"
)

type BurnActionSpec struct {
}

func (*BurnActionSpec) Name() string {
	return "burn"
}

func (*BurnActionSpec) Aliases() []string {
	return []string{}
}
func (*BurnActionSpec) ShortDesc() string {
	return "Increase disk read and write io load"
}

func (*BurnActionSpec) LongDesc() string {
	return "Increase disk read and write io load"
}

func (*BurnActionSpec) Matchers() []exec.ExpFlagSpec {
	return []exec.ExpFlagSpec{
		&exec.ExpFlag{
			Name:   "read",
			Desc:   "Burn io by read, it will create a 600M for reading and delete it when destroy it",
			NoArgs: true,
		},
		&exec.ExpFlag{
			Name:   "write",
			Desc:   "Burn io by write, it will create a file by value of the size flag, for example the size default value is 10, then it will create a 10M*100=1000M file for writing, and delete it when destroy",
			NoArgs: true,
		},
	}
}

func (*BurnActionSpec) Flags() []exec.ExpFlagSpec {
	return []exec.ExpFlagSpec{
		&exec.ExpFlag{
			Name: "size",
			Desc: "Block size, MB, default is 10",
		},
		&exec.ExpFlag{
			Name: "path",
			Desc: "The path of directory where the disk is burning, default value is /",
		},
	}
}

type BurnIOExecutor struct {
	channel exec.Channel
}

func (*BurnIOExecutor) Name() string {
	return "burn"
}

var burnIOBin = "chaos_burnio"

func (be *BurnIOExecutor) Exec(uid string, ctx context.Context, model *exec.ExpModel) *transport.Response {
	if be.channel == nil {
		return transport.ReturnFail(transport.Code[transport.ServerError], "channel is nil")
	}
	directory := "/"
	path := model.ActionFlags["path"]
	if path != "" {
		directory = path
	}
	if _, ok := exec.IsDestroy(ctx); ok {
		readExists := model.ActionFlags["read"] == "true"
		writeExists := model.ActionFlags["write"] == "true"
		return be.stop(ctx, readExists, writeExists, directory)
	}
	if !util.IsDir(directory) {
		return transport.ReturnFail(transport.Code[transport.IllegalParameters],
			fmt.Sprintf("the %s path must be directory", directory))
	}
	readExists := model.ActionFlags["read"] == "true"
	writeExists := model.ActionFlags["write"] == "true"
	if !readExists && !writeExists {
		return transport.ReturnFail(transport.Code[transport.IllegalParameters], "less --read or --write flag")
	}
	size := model.ActionFlags["size"]
	if size == "" {
		size = "10"
	}
	return be.start(ctx, readExists, writeExists, directory, size)
}

func (be *BurnIOExecutor) start(ctx context.Context, read, write bool, directory, size string) *transport.Response {
	return be.channel.Run(ctx, path.Join(be.channel.GetScriptPath(), burnIOBin),
		fmt.Sprintf("--read=%t --write=%t --directory %s --size %s --start", read, write, directory, size))
}

func (be *BurnIOExecutor) stop(ctx context.Context, read, write bool, directory string) *transport.Response {
	return be.channel.Run(ctx, path.Join(be.channel.GetScriptPath(), burnIOBin),
		fmt.Sprintf("--read=%t --write=%t --directory %s --stop", read, write, directory))
}

func (be *BurnIOExecutor) SetChannel(channel exec.Channel) {
	be.channel = channel
}

func (*BurnActionSpec) Executor(channel exec.Channel) exec.Executor {
	return &BurnIOExecutor{channel: channel}
}
