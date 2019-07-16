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
		fileSystem string
		size       string
		count      string
		read       bool
		write      bool
	}

	burnBin := path.Join(util.GetProgramPath(), "chaos_burnio")
	as := &args{
		fileSystem: "/dev/disk1s1",
		size:       "1024",
		count:      "1024",
		read:       true,
		write:      true,
	}

	var exitCode int
	bin.ExitFunc = func(code int) {
		exitCode = code
	}
	var invokeTime int
	stopBurnIOFunc = func() {
		invokeTime++
	}
	channel = &exec.MockLocalChannel{
		Response:        transport.ReturnFail(transport.Code[transport.CommandNotFound], "nohup command not found"),
		ExpectedCommand: fmt.Sprintf(`nohup %s --file-system /dev/disk1s1 --size 1024 --count 1024 --read=true --write=true --nohup=true > /tmp/chaos_burnio.log 2>&1 &`, burnBin),
		T:               t,
	}

	startBurnIO(as.fileSystem, as.size, as.count, as.read, as.write)
	if exitCode != 1 {
		t.Errorf("unexpected result: %d, expected result: %d", exitCode, 1)
	}
	if invokeTime != 1 {
		t.Errorf("unexpected invoke time %d, expected result: %d", invokeTime, 1)
	}
}

func Test_stopBurnIO(t *testing.T) {
	tests := []struct {
		name string
	}{
		{"stop"},
	}
	channel = exec.NewLocalChannel()
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			stopBurnIO()
		})
	}
}

func Test_getFileSystem(t *testing.T) {
	type args struct {
		mountPoint string
	}
	tests := []struct {
		name    string
		args    args
		wantErr bool
	}{
		{"test1", args{"/"}, false},
		{"test2", args{"/dev"}, false},
		{"test2", args{"devs"}, false},
	}
	channel = exec.NewLocalChannel()
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			_, err := getFileSystem(tt.args.mountPoint)
			if (err != nil) != tt.wantErr {
				t.Errorf("getFileSystem() error = %v, wantErr %v", err, tt.wantErr)
				return
			}
		})
	}
}
