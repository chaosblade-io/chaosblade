package main

import (
	"testing"
	"net"
)

func TestStartAndStopDelayNet(t *testing.T) {
	t.Skip("")
	interfaces, err := net.Interfaces()
	if err != nil || len(interfaces) == 0 {
		t.Error("no avaliable net interface")
	}

	inputs := []struct {
		netInterface, time, offset, localPort, remotePort, excludePort string
	} {
		{interfaces[0].Name, "1000", "10", "", "", ""},
		{interfaces[0].Name, "1000", "10", "", "", "15355"},
		{interfaces[0].Name, "2000", "10", "80", "", ""},
		{interfaces[0].Name, "3000", "10", "", "80", ""},
		{interfaces[0].Name, "4000", "10", "80", "80", ""},
	}

	for _, it := range inputs {
		startDelayNet(it.netInterface, it.time, it.offset, it.localPort, it.remotePort, it.excludePort)
		stopDelayNet(it.netInterface)
	}
}