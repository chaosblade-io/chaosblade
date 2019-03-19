package os

import (
	"github.com/chaosblade-io/chaosblade/exec"
	"github.com/chaosblade-io/chaosblade/transport"
	"context"
	"path"
	"fmt"
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
	return "Burn io by read or write"
}

func (*BurnActionSpec) LongDesc() string {
	return "Burn io by read or write"
}

func (*BurnActionSpec) Matchers() []exec.ExpFlagSpec {
	return []exec.ExpFlagSpec{
		&exec.ExpFlag{
			Name:   "read",
			Desc:   "Burn io by read",
			NoArgs: true,
		},
		&exec.ExpFlag{
			Name:   "write",
			Desc:   "Burn io by write",
			NoArgs: true,
		},
	}
}

func (*BurnActionSpec) Flags() []exec.ExpFlagSpec {
	return []exec.ExpFlagSpec{
		&exec.ExpFlag{
			Name: "count",
			Desc: "File count, default is 1024",
		},
		&exec.ExpFlag{
			Name: "size",
			Desc: "Block size, MB, default is 1MB",
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
	mountOn := model.ActionFlags["mount-on"]
	if mountOn == "" {
		mountOn = "/"
	}
	if _, ok := exec.IsDestroy(ctx); ok {
		return be.stop(ctx)
	} else {
		readExists := model.ActionFlags["read"] == "true"
		writeExists := model.ActionFlags["write"] == "true"
		if !readExists && !writeExists {
			return transport.ReturnFail(transport.Code[transport.IllegalParameters], "less --read or --write flag")
		}
		count := model.ActionFlags["count"]
		if count == "" {
			count = "1024"
		}
		size := model.ActionFlags["size"]
		if size == "" {
			size = "1"
		}
		return be.start(readExists, writeExists, count, size, mountOn, ctx)
	}
}

func (be *BurnIOExecutor) start(read, write bool, count, size, device string, ctx context.Context) *transport.Response {
	return be.channel.Run(ctx, path.Join(be.channel.GetScriptPath(), burnIOBin),
		fmt.Sprintf("--read=%t --write=%t --count %s --size %s --device %s --start", read, write, count, size, device))
}

func (be *BurnIOExecutor) stop(ctx context.Context) *transport.Response {
	return be.channel.Run(ctx, path.Join(be.channel.GetScriptPath(), burnIOBin), "--stop")
}

func (be *BurnIOExecutor) SetChannel(channel exec.Channel) {
	be.channel = channel
}

func (*BurnActionSpec) Executor(channel exec.Channel) exec.Executor {
	return &BurnIOExecutor{channel: channel}
}
