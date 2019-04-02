package main

import (
	"fmt"
	"github.com/chaosblade-io/chaosblade/transport"
	"flag"
	"github.com/chaosblade-io/chaosblade/exec"
	"context"
)

var dropLocalPort, dropRemotePort string
var dropNetStart, dropNetStop bool

func main() {
	flag.StringVar(&dropLocalPort, "local-port", "", "local port")
	flag.StringVar(&dropRemotePort, "remote-port", "", "remote port")
	flag.BoolVar(&dropNetStart, "start", false, "start drop")
	flag.BoolVar(&dropNetStop, "stop", false, "stop drop")
	flag.Parse()

	if dropNetStart == dropNetStop {
		printErrAndExit("must add --start or --stop flag")
	}
	if dropNetStart {
		startDropNet(dropLocalPort, dropRemotePort)
	} else if dropNetStop {
		stopDropNet(dropLocalPort, dropRemotePort)
	} else {
		printErrAndExit("less --start or --stop flag")
	}
}

func startDropNet(localPort, remotePort string) {
	channel := exec.NewLocalChannel()
	ctx := context.Background()
	if remotePort == "" && localPort == "" {
		printErrAndExit("must specify port flag")
	}
	handleDropSpecifyPort(remotePort, localPort, channel, ctx)
}

func handleDropSpecifyPort(remotePort string, localPort string, channel *exec.LocalChannel, ctx context.Context) {
	var response *transport.Response
	if localPort != "" {
		response = channel.Run(ctx, "iptables",
			fmt.Sprintf(`-A INPUT -p tcp --dport %s -j DROP`, localPort))
		if !response.Success {
			stopDropNet(localPort, remotePort)
			printErrAndExit(response.Err)
		}
		response = channel.Run(ctx, "iptables",
			fmt.Sprintf(`-A INPUT -p udp --dport %s -j DROP`, localPort))
		if !response.Success {
			stopDropNet(localPort, remotePort)
			printErrAndExit(response.Err)
		}
	}
	if remotePort != "" {
		response = channel.Run(ctx, "iptables",
			fmt.Sprintf(`-A OUTPUT -p tcp --dport %s -j DROP`, localPort))
		if !response.Success {
			stopDropNet(localPort, remotePort)
			printErrAndExit(response.Err)
		}
		response = channel.Run(ctx, "iptables",
			fmt.Sprintf(`-A OUTPUT -p udp --dport %s -j DROP`, localPort))
		if !response.Success {
			stopDropNet(localPort, remotePort)
			printErrAndExit(response.Err)
		}
	}
	printOutputAndExit(response.Result.(string))
}

func stopDropNet(localPort, remotePort string) {
	channel := exec.NewLocalChannel()
	ctx := context.Background()
	if localPort != "" {
		channel.Run(ctx, "iptables", fmt.Sprintf(`-D INPUT -p tcp --dport %s -j DROP`, localPort))
		channel.Run(ctx, "iptables", fmt.Sprintf(`-D INPUT -p udp --dport %s -j DROP`, localPort))
	}
	if remotePort != "" {
		channel.Run(ctx, "iptables", fmt.Sprintf(`-D OUTPUT -p tcp --dport %s -j DROP`, localPort))
		channel.Run(ctx, "iptables", fmt.Sprintf(`-D OUTPUT -p udp --dport %s -j DROP`, localPort))
	}
}
