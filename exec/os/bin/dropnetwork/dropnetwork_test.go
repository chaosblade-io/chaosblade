package main

import (
	"context"
	"testing"

	"github.com/chaosblade-io/chaosblade/exec"
	"github.com/chaosblade-io/chaosblade/exec/os/bin"
	"github.com/chaosblade-io/chaosblade/transport"
)

func Test_startDropNet_failed(t *testing.T) {
	var exitCode int
	bin.ExitFunc = func(code int) {
		exitCode = code
	}
	tests := []struct {
		localPort  string
		remotePort string
	}{
		{"", ""},
	}

	for _, tt := range tests {
		startDropNet(tt.localPort, tt.remotePort)
		if exitCode != 1 {
			t.Errorf("unexpected result: %d, expected result: %d", exitCode, 1)
		}
	}
}

func Test_handleDropSpecifyPort(t *testing.T) {
	type input struct {
		localPort  string
		remotePort string
		response   *transport.Response
	}
	type expect struct {
		exitCode   int
		invokeTime int
	}

	tests := []struct {
		input  input
		expect expect
	}{
		{input{"80", "", transport.ReturnFail(transport.Code[transport.CommandNotFound], "iptables command not found")},
			expect{1, 1}},
		{input{"", "80", transport.ReturnFail(transport.Code[transport.CommandNotFound], "iptables command not found")},
			expect{1, 1}},
		{input{"80", "", transport.ReturnSuccess("success")},
			expect{0, 0}},
	}

	var exitCode int
	bin.ExitFunc = func(code int) {
		exitCode = code
	}
	var invokeTime int
	stopDropNetFunc = func(localPort, remotePort string) {
		invokeTime++
	}
	for _, tt := range tests {
		invokeTime = 0
		channel = &exec.MockLocalChannel{
			Response:     tt.input.response,
			NoCheck:	  true,
			T:            t,
		}
		handleDropSpecifyPort(tt.input.remotePort, tt.input.localPort, channel, context.Background())
		if exitCode != tt.expect.exitCode {
			t.Errorf("unexpected result: %d, expected result: %d", exitCode, tt.expect.exitCode)
		}
		if invokeTime != tt.expect.invokeTime {
			t.Errorf("unexpected invoke time %d, expected result: %d", invokeTime, tt.expect.invokeTime)
		}
	}
}
