package main

import (
	"testing"
	"context"
	"fmt"

	"github.com/chaosblade-io/chaosblade/exec"
)

func TestChangeAndRecoverDns(t *testing.T) {
	t.Skip("")
	inputs := []struct {
		domain  string
		ip      string
	}{
		{"appspot.com", "172.217.168.209"},	
		{"www.bbc.com", "151.101.8.81"},	
		{"behance.com", "216.146.46.10"},
		{"app.box.com",	"107.152.24.198"},	
		{"img.buzzfeed.com", "151.101.2.114"},
	}
	for _, it := range inputs {
		startChangeDns(it.domain, it.ip)
	}
	for _, it := range inputs {
		recoverDns(it.domain, it.ip)
	}
	for _, it := range inputs {
		channel := exec.NewLocalChannel()
		ctx := context.Background()
		dnsPair := createDnsPair(it.domain, it.ip)
		response := channel.Run(ctx, "grep", fmt.Sprintf(`-q "%s" %s`, dnsPair, hosts))
		if response.Success {
			t.Errorf("unexpected result: %v, expected: %s", it, "")
		}
	}
}