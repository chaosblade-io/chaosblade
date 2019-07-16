package main

import (
	"context"
	"flag"
	"fmt"

	"github.com/chaosblade-io/chaosblade/exec"
	"github.com/chaosblade-io/chaosblade/exec/os/bin"
	"github.com/chaosblade-io/chaosblade/transport"
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
		bin.PrintErrAndExit("must add --start or --stop flag")
	}
	if dropNetStart {
		startDropNet(dropLocalPort, dropRemotePort)
	} else if dropNetStop {
		stopDropNet(dropLocalPort, dropRemotePort)
	} else {
		bin.PrintErrAndExit("less --start or --stop flag")
	}
}

var channel = exec.NewLocalChannel()

var stopDropNetFunc = stopDropNet

func startDropNet(localPort, remotePort string) {
	ctx := context.Background()
	if remotePort == "" && localPort == "" {
		bin.PrintErrAndExit("must specify port flag")
		return
	}
	handleDropSpecifyPort(remotePort, localPort, channel, ctx)
}

func handleDropSpecifyPort(remotePort string, localPort string, channel exec.Channel, ctx context.Context) {
	var response *transport.Response
	if localPort != "" {
		response = channel.Run(ctx, "iptables",
			fmt.Sprintf(`-A INPUT -p tcp --dport %s -j DROP`, localPort))
		if !response.Success {
			stopDropNetFunc(localPort, remotePort)
			bin.PrintErrAndExit(response.Err)
			return
		}
		response = channel.Run(ctx, "iptables",
			fmt.Sprintf(`-A INPUT -p udp --dport %s -j DROP`, localPort))
		if !response.Success {
			stopDropNetFunc(localPort, remotePort)
			bin.PrintErrAndExit(response.Err)
			return
		}
	}
	if remotePort != "" {
		response = channel.Run(ctx, "iptables",
			fmt.Sprintf(`-A OUTPUT -p tcp --dport %s -j DROP`, remotePort))
		if !response.Success {
			stopDropNetFunc(localPort, remotePort)
			bin.PrintErrAndExit(response.Err)
			return
		}
		response = channel.Run(ctx, "iptables",
			fmt.Sprintf(`-A OUTPUT -p udp --dport %s -j DROP`, remotePort))
		if !response.Success {
			stopDropNetFunc(localPort, remotePort)
			bin.PrintErrAndExit(response.Err)
			return
		}
	}
	bin.PrintOutputAndExit(response.Result.(string))
}

func stopDropNet(localPort, remotePort string) {
	ctx := context.Background()
	if localPort != "" {
		channel.Run(ctx, "iptables", fmt.Sprintf(`-D INPUT -p tcp --dport %s -j DROP`, localPort))
		channel.Run(ctx, "iptables", fmt.Sprintf(`-D INPUT -p udp --dport %s -j DROP`, localPort))
	}
	if remotePort != "" {
		channel.Run(ctx, "iptables", fmt.Sprintf(`-D OUTPUT -p tcp --dport %s -j DROP`, remotePort))
		channel.Run(ctx, "iptables", fmt.Sprintf(`-D OUTPUT -p udp --dport %s -j DROP`, remotePort))
	}
}
