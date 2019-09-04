package main

import (
	"context"
	"fmt"
	"path"
	"testing"

	"github.com/chaosblade-io/chaosblade/exec"
	"github.com/chaosblade-io/chaosblade/exec/os/bin"
	"github.com/chaosblade-io/chaosblade/transport"
	"github.com/chaosblade-io/chaosblade/util"
)

func Test_startBurnMem(t *testing.T) {

	var exitCode int
	bin.ExitFunc = func(code int) {
		exitCode = code
	}

	runBurnMemFunc = func(context.Context, int) int {
		return 1
	}

	stopBurnMemFunc = func() (bool, string) {
		return true, ""
	}

	flPath := path.Join(util.GetProgramPath(), dirName)
	channel = &exec.MockLocalChannel{
		Response:         transport.ReturnSuccess("success"),
		ExpectedCommands: []string{fmt.Sprintf("mount -t tmpfs tmpfs %s -o size=", flPath) + "100%"},
		T:                t,
	}

	startBurnMem()
	if exitCode != 0 {
		t.Errorf("unexpected result %d, expected result: %d", exitCode, 0)
	}

}

func Test_runBurnMem_failed(t *testing.T) {
	type args struct {
		memPercent int
	}
	as := &args{
		memPercent: 50,
	}

	burnBin := path.Join(util.GetProgramPath(), "chaos_burnmem")
	var exitCode int
	bin.ExitFunc = func(code int) {
		exitCode = code
	}

	channel = &exec.MockLocalChannel{
		Response:         transport.ReturnFail(transport.Code[transport.CommandNotFound], "nohup command not found"),
		ExpectedCommands: []string{fmt.Sprintf(`nohup %s --nohup --mem-percent 50 > /dev/null 2>&1 &`, burnBin)},
		T:                t,
	}

	stopBurnMemFunc = func() (bool, string) {
		return true, ""
	}

	runBurnMem(context.Background(), as.memPercent)

	if exitCode != 1 {
		t.Errorf("unexpected result %d, expected result: %d", exitCode, 1)
	}

}

func Test_stopBurnMem(t *testing.T) {
	tests := []struct {
		name string
	}{
		{"stop"},
	}
	channel = exec.NewLocalChannel()
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			stopBurnMem()
		})
	}
}
