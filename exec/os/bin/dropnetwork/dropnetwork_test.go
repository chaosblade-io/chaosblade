package main

import (
	"testing"
)

func TestStartAndDropNet(t *testing.T) {
	t.Skip("")
	inputs := []struct {
		localPort, remotePort string
	}{
		{"80", ""},
		{"", "80"},
	}

	for _, it := range inputs {
		startDropNet(it.localPort, it.remotePort)
		stopDropNet(it.localPort, it.remotePort)
	}
}