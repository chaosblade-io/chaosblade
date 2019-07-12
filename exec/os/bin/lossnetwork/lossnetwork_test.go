package main

import (
	"testing"
	"net"
)

func TestStartAndStopLossNet(t *testing.T) {
	t.Skip("")
	interfaces, err := net.Interfaces()
	if err != nil || len(interfaces) == 0 {
		t.Error("no avaliable net interface")
	}

	inputs := []struct {
		netInterface, percent, localPort, remotePort, excludePort string
	} {
		{interfaces[0].Name, "2", "", "", ""},
		{interfaces[0].Name, "3", "", "", "15355"},
		{interfaces[0].Name, "4", "80", "", ""},
		{interfaces[0].Name, "5", "", "80", ""},
		{interfaces[0].Name, "6", "80", "80", ""},
	}

	for _, it := range inputs {
		startLossNet(it.netInterface, it.percent, it.localPort, it.remotePort, it.excludePort)
		stopLossNet(it.netInterface)
	}
}