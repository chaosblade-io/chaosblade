package main

import (
	"testing"

	"github.com/chaosblade-io/chaosblade/exec"
	"github.com/chaosblade-io/chaosblade/exec/os/bin"
	"github.com/chaosblade-io/chaosblade/transport"
)

func Test_startFill_startSuccessful(t *testing.T) {
	type args struct {
		mountPoint string
		size       string
	}
	as := &args{
		mountPoint: "/dev",
		size:       "10",
	}

	var exitCode int
	bin.ExitFunc = func(code int) {
		exitCode = code
	}

	channel = &exec.MockLocalChannel{
		Response: transport.ReturnSuccess("success"),
		NoCheck:  true,
		T:        t,
	}

	startFill(as.mountPoint, as.size)
	if exitCode != 0 {
		t.Errorf("unexpected result %d, expected result: %d", exitCode, 1)
	}
}

func Test_stopFill(t *testing.T) {
	channel = &exec.MockLocalChannel{
		Response: transport.ReturnSuccess("success"),
		NoCheck:  true,
		T:        t,
	}
	bin.ExitFunc = func(code int) {}
	type args struct {
		mountPoint string
	}
	tests := []struct {
		name    string
		args    args
	}{
		{"stop", args{"/dev"}},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			stopFill(tt.args.mountPoint)
		})
	}
}