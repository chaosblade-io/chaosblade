package main

import (
	"fmt"
	"testing"

	"github.com/chaosblade-io/chaosblade/exec"
	"github.com/chaosblade-io/chaosblade/exec/os/bin"
	"github.com/chaosblade-io/chaosblade/transport"
)

func Test_createDnsPair(t *testing.T) {
	type input struct {
		domain string
		ip     string
	}
	tests := []struct {
		input  input
		expect string
	}{
		{input{"bbc.com", "151.101.8.81"}, "151.101.8.81 bbc.com #chaosblade"},
		{input{"g.com", "172.217.168.209"}, "172.217.168.209 g.com #chaosblade"},
		{input{"github.com", "192.30.255.112"}, "192.30.255.112 github.com #chaosblade"},
	}

	for _, tt := range tests {
		got := createDnsPair(tt.input.domain, tt.input.ip)
		if got != tt.expect {
			t.Errorf("unexpected result: %s, expected result: %s", got, tt.expect)
		}
	}
}
func Test_startChangeDns_failed(t *testing.T) {
	type args struct {
		domain string
		ip     string
	}

	as := &args{
		domain: "abc.com",
		ip:     "208.80.152.2",
	}

	var exitCode int
	bin.ExitFunc = func(code int) {
		exitCode = code
	}
	channel = &exec.MockLocalChannel{
		Response:         transport.ReturnSuccess("DnsPair has exists"),
		ExpectedCommands: []string{fmt.Sprintf(`grep -q "208.80.152.2 abc.com #chaosblade" /etc/hosts`)},
		T:                t,
	}

	startChangeDns(as.domain, as.ip)
	if exitCode != 1 {
		t.Errorf("unexpected result %d, expected result: %d", exitCode, 1)
	}
}

func Test_recoverDns_failed(t *testing.T) {
	type args struct {
		domain string
		ip     string
	}

	as := &args{
		domain: "abc.com",
		ip:     "208.80.152.2",
	}

	var exitCode int
	bin.ExitFunc = func(code int) {
		exitCode = code
	}
	channel = &exec.MockLocalChannel{
		Response:         transport.ReturnFail(transport.Code[transport.CommandNotFound], "grep command not found"),
		ExpectedCommands: []string{fmt.Sprintf(`grep -q "208.80.152.2 abc.com #chaosblade" /etc/hosts`)},
		T:                t,
	}

	recoverDns(as.domain, as.ip)
	if exitCode != 0 {
		t.Errorf("unexpected result %d, expected result: %d", exitCode, 1)
	}
}
