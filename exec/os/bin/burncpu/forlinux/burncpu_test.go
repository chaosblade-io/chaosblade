package main

import (
	"context"
	"fmt"
	"path"
	"testing"

	"github.com/containerd/cgroups"

	"github.com/chaosblade-io/chaosblade/exec"
	"github.com/chaosblade-io/chaosblade/exec/os/bin"
	"github.com/chaosblade-io/chaosblade/transport"
	"github.com/chaosblade-io/chaosblade/util"
)

func Test_startBurnCpu(t *testing.T) {
	type args struct {
		cpuList  string
		cpuCount int
	}
	tests := []struct {
		name string
		args args
	}{
		{"test1", args{"1,2,3,5", 0}},
		{"test2", args{"", 3}},
	}
	runBurnCpuFunc = func(ctx context.Context, cpuCount int, pidNeeded bool, processor string) int {
		return 25233
	}
	bindBurnCpuFunc = func(cgctrl cgroups.Cgroup, cpulist string) {}
	checkBurnCpuFunc = func(ctx context.Context) {}
	cgroupNewFunc = func(int, int) cgroups.Cgroup { return new(CgroupMock) }
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			cpuList = tt.args.cpuList
			cpuCount = tt.args.cpuCount
			startBurnCpu()
		})
	}
}
func Test_runBurnCpu_failed(t *testing.T) {
	type args struct {
		cpuCount  int
		pidNeeded bool
		processor string
	}
	burnBin := path.Join(util.GetProgramPath(), "chaos_burncpu")
	as := &args{
		cpuCount:  2,
		pidNeeded: false,
		processor: "",
	}

	var exitCode int
	bin.ExitFunc = func(code int) {
		exitCode = code
	}
	var invokeTime int
	stopBurnCpuFunc = func() (bool, string) {
		invokeTime++
		return true, ""
	}

	channel = &exec.MockLocalChannel{
		Response:         transport.ReturnFail(transport.Code[transport.CommandNotFound], "nohup command not found"),
		ExpectedCommands: []string{fmt.Sprintf(`nohup %s --nohup --cpu-count 2 > /dev/null 2>&1 &`, burnBin)},
		T:                t,
	}

	runBurnCpu(context.Background(), as.cpuCount, as.pidNeeded, as.processor)
	if exitCode != 1 {
		t.Errorf("unexpected result %d, expected result: %d", exitCode, 1)
	}
	if invokeTime != 1 {
		t.Errorf("unexpected invoke time %d, expected result: %d", invokeTime, 1)
	}
}

func Test_bindBurnCpu(t *testing.T) {
	type args struct {
		core string
		pid  int
	}
	as := &args{
		core: "0",
		pid:  25233,
	}

	var exitCode int
	bin.ExitFunc = func(code int) {
		exitCode = code
	}
	stopBurnCpuFunc = func() (bool, string) { return true, "" }

	channel = &exec.MockLocalChannel{
		Response:         transport.ReturnFail(transport.Code[transport.CommandNotFound], "taskset command not found"),
		ExpectedCommands: []string{fmt.Sprintf(`taskset -a -cp 0 25233`)},
		T:                t,
	}

	bindBurnCpuByTaskset(context.Background(), as.core, as.pid)
	if exitCode != 1 {
		t.Errorf("unexpected result %d, expected result: %d", exitCode, 1)
	}
}
func Test_checkBurnCpu(t *testing.T) {
	var exitCode int
	bin.ExitFunc = func(code int) {
		exitCode = code
	}
	checkBurnCpu(context.Background())
	if exitCode != 1 {
		t.Errorf("unexpected result %d, expected result: %d", exitCode, 1)
	}
}

func Test_stopBurnCpu(t *testing.T) {
	tests := []struct {
		name string
	}{
		{"stop"},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			cgroupNew(2, 50)
			stopBurnCpu()
		})
	}
}
