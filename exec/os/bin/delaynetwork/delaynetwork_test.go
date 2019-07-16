package main

import (
	"fmt"
	"testing"
	"context"

	"github.com/chaosblade-io/chaosblade/exec"
	"github.com/chaosblade-io/chaosblade/exec/os/bin"
	"github.com/chaosblade-io/chaosblade/transport"
)

func Test_startDelayNet(t *testing.T) {
	type args struct {
		netInterface string
		time         string
		offset       string
		localPort    string
		remotePort   string
		excludePort  string
	}

	as := &args{
		netInterface: "eth0",
		time:         "3000",
		offset:       "10",
		localPort:    "",
		remotePort:   "",
		excludePort:  "",
	}

	var exitCode int
	bin.ExitFunc = func(code int) {
		exitCode = code
	}
	channel = &exec.MockLocalChannel{
		Response:        transport.ReturnSuccess("success"),
		ExpectedCommand: fmt.Sprintf(`tc qdisc add dev eth0 root netem delay 3000ms 10ms`),
		T:               t,
	}

	startDelayNet(as.netInterface, as.time, as.offset, as.localPort, as.remotePort, as.excludePort)
	if exitCode != 0 {
		t.Errorf("unexpected result: %d, expected result: %d", exitCode, 1)
	}
}

func Test_addLocalOrRemotePortForDelay(t *testing.T) {
	type input struct {
		localPort		string
		remotePort		string
		netInterface	string
		response 		*transport.Response 
		expectedCommand string
	}
	type expect struct {
		exitCode		int
		invokeTime		int
	}

	tests := []struct {
		input 			input 
		expect 			expect 
	}{
		{input{"80", "", "eth0", transport.ReturnSuccess("success"), 
				`tc filter add dev eth0 parent 1: protocol ip prio 4 basic match "cmp(u16 at 0 layer transport eq 80)" flowid 1:4`}, 
		 expect{0, 0}},
		{input{"", "80", "eth0", transport.ReturnSuccess("success"), 
				`tc filter add dev eth0 parent 1: protocol ip prio 4 basic match "cmp(u16 at 2 layer transport eq 80)" flowid 1:4`}, 
		 expect{0, 0}},
		{input{"80", "", "eth0", transport.ReturnFail(transport.Code[transport.CommandNotFound], "tc command not found"), 
				`tc filter add dev eth0 parent 1: protocol ip prio 4 basic match "cmp(u16 at 0 layer transport eq 80)" flowid 1:4`}, 
		 expect{1, 1}},
	}

	var exitCode int
	bin.ExitFunc = func(code int) {
		exitCode = code
	}
	var invokeTime int
	stopDelayNetFunc = func(netInterface string) {
		invokeTime++
	}
	for _, tt := range tests {
		invokeTime = 0
		channel = &exec.MockLocalChannel{
			Response:        tt.input.response,
			ExpectedCommand: tt.input.expectedCommand,
			T:               t,
		}
		addLocalOrRemotePortForDelay(tt.input.localPort, &transport.Response{}, channel, context.Background(), tt.input.netInterface, tt.input.remotePort)
		if exitCode != tt.expect.exitCode {
			t.Errorf("unexpected result: %d, expected result: %d", exitCode, tt.expect.exitCode)
		}
		if invokeTime != tt.expect.invokeTime {
			t.Errorf("unexpected invoke time %d, expected result: %d", invokeTime, tt.expect.invokeTime)
		}
	}
}

func Test_addExcludePortFilterForDelay(t *testing.T) {
	type input struct {
		excludePort		string
		netInterface	string
		response 		*transport.Response 
		expectedCommand string
	}
	type expect struct {
		exitCode		int
		invokeTime		int
	}

	tests := []struct {
		input 			input 
		expect 			expect 
	}{
		{input{"80", "eth0", transport.ReturnFail(transport.Code[transport.CommandNotFound], "tc command not found"), 
				`tc filter add dev eth0 parent 1: protocol ip prio 4 basic match "cmp(u16 at 0 layer transport gt 0) and cmp(u16 at 0 layer transport lt 80)" flowid 1:4`}, 
		 expect{1, 1}},
	}

	var exitCode int
	bin.ExitFunc = func(code int) {
		exitCode = code
	}
	var invokeTime int
	stopDelayNetFunc = func(netInterface string) {
		invokeTime++
	}
	for _, tt := range tests {
		invokeTime = 0
		channel = &exec.MockLocalChannel{
			Response:        tt.input.response,
			ExpectedCommand: tt.input.expectedCommand,
			T:               t,
		}
		addExcludePortFilterForDelay(tt.input.excludePort, tt.input.netInterface, &transport.Response{}, channel, context.Background())
		if exitCode != tt.expect.exitCode {
			t.Errorf("unexpected result: %d, expected result: %d", exitCode, tt.expect.exitCode)
		}
		if invokeTime != tt.expect.invokeTime {
			t.Errorf("unexpected invoke time %d, expected result: %d", invokeTime, tt.expect.invokeTime)
		}
	}
}

func Test_addQdiscForDelay(t *testing.T) {
	type args struct {
		netInterface 	string
		time 			string
		offset 			string
	}
	as := &args{
		netInterface:	"eth0",
		time:			"3000",
		offset: 		"10",
	}

	var exitCode int
	bin.ExitFunc = func(code int) {
		exitCode = code
	}
	channel = &exec.MockLocalChannel{
		Response:        transport.ReturnFail(transport.Code[transport.CommandNotFound], "tc command not found"),
		ExpectedCommand: fmt.Sprintf(`tc qdisc add dev eth0 root handle 1: prio bands 4`),
		T:               t,
	}

	addQdiscForDelay(channel, context.Background(), as.netInterface, as.time, as.offset)
	if exitCode != 1 {
		t.Errorf("unexpected result: %d, expected result: %d", exitCode, 1)
	}
}