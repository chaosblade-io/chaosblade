package main

import (
	"fmt"
	"path"
	"testing"

	"github.com/chaosblade-io/chaosblade/exec"
	"github.com/chaosblade-io/chaosblade/exec/os/bin"
	"github.com/chaosblade-io/chaosblade/transport"
	"github.com/chaosblade-io/chaosblade/util"
)

func Test_startBurnIO_startFailed(t *testing.T) {
	type args struct {
		directory string
		size      string
		read      bool
		write     bool
	}

	burnBin := path.Join(util.GetProgramPath(), "chaos_burnio")
	as := &args{
		directory: "/home/admin",
		size:      "1024",
		read:      true,
		write:     true,
	}

	var exitCode int
	bin.ExitFunc = func(code int) {
		exitCode = code
	}
	var invokeTime int
	stopBurnIOFunc = func(directory string, read, write bool) {
		invokeTime++
	}
	channel = &exec.MockLocalChannel{
		Response:         transport.ReturnFail(transport.Code[transport.CommandNotFound], "nohup command not found"),
		ExpectedCommands: []string{fmt.Sprintf(`nohup %s --directory /home/admin --size 1024 --read=true --write=true --nohup=true > %s 2>&1 &`, burnBin, logFile)},
		T:                t,
	}

	startBurnIO(as.directory, as.size, as.read, as.write)
	if exitCode != 1 {
		t.Errorf("unexpected result: %d, expected result: %d", exitCode, 1)
	}
	if invokeTime != 1 {
		t.Errorf("unexpected invoke time %d, expected result: %d", invokeTime, 1)
	}
}

func Test_stopBurnIO(t *testing.T) {
	tests := []struct {
		name      string
		directory string
		read      bool
		write     bool
	}{
		{
			name:      "stop",
			directory: "/home/admin",
			read:      true,
			write:     true,
		},
	}
	channel = exec.NewLocalChannel()
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			stopBurnIO(tt.directory, tt.read, tt.write)
		})
	}
}
