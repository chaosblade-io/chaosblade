package main

import (
	"testing"

	"github.com/chaosblade-io/chaosblade/exec/os/bin"
)

func Test_killProcess(t *testing.T) {
	type args struct {
		process    string
		processCmd string
	}
	as := &args{
		process:    "",
		processCmd: "",
	}

	var exitCode int
	bin.ExitFunc = func(code int) {
		exitCode = code
	}

	killProcess(as.process, as.processCmd)
	if exitCode != 1 {
		t.Errorf("unexpected result %d, expected result: %d", exitCode, 1)
	}
}
