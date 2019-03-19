package main

import (
	"fmt"
	"github.com/chaosblade-io/chaosblade/transport"
	"flag"
	"github.com/chaosblade-io/chaosblade/exec"
	"context"
)

var dropServicePort, dropInvokePort string
var dropNetStart, dropNetStop bool

func main() {
	flag.StringVar(&dropServicePort, "service-port", "", "service port")
	flag.StringVar(&dropInvokePort, "invoke-port", "", "invoke port")
	flag.BoolVar(&dropNetStart, "start", false, "start drop")
	flag.BoolVar(&dropNetStop, "stop", false, "stop drop")
	flag.Parse()

	if dropNetStart == dropNetStop {
		printErrAndExit("must add --start or --stop flag")
	}
	if dropNetStart {
		startDropNet(dropServicePort, dropInvokePort)
	} else if dropNetStop {
		stopDropNet(dropServicePort, dropInvokePort)
	} else {
		printErrAndExit("less --start or --stop flag")
	}
}

func startDropNet(servicePort, invokePort string) {
	channel := exec.NewLocalChannel()
	ctx := context.Background()
	handleDropAllPort(invokePort, servicePort, channel, ctx)
	handleDropSpecifyPort(invokePort, servicePort, channel, ctx)
}

func handleDropSpecifyPort(invokePort string, servicePort string, channel *exec.LocalChannel, ctx context.Context) {
	var response *transport.Response
	if servicePort != "" {
		response = channel.Run(ctx, "iptables",
			fmt.Sprintf(`-A INPUT -p tcp --dport %s -j DROP`, servicePort))
		if !response.Success {
			stopDropNet(servicePort, invokePort)
			printErrAndExit(response.Err)
		}
		response = channel.Run(ctx, "iptables",
			fmt.Sprintf(`-A INPUT -p tcp --dport %s -j DROP`, servicePort))
		if !response.Success {
			stopDropNet(servicePort, invokePort)
			printErrAndExit(response.Err)
		}
	}
	if invokePort != "" {
		response = channel.Run(ctx, "iptables",
			fmt.Sprintf(`-A OUTPUT -p tcp --dport %s -j DROP`, servicePort))
		if !response.Success {
			stopDropNet(servicePort, invokePort)
			printErrAndExit(response.Err)
		}
		response = channel.Run(ctx, "iptables",
			fmt.Sprintf(`-A OUTPUT -p tcp --dport %s -j DROP`, servicePort))
		if !response.Success {
			stopDropNet(servicePort, invokePort)
			printErrAndExit(response.Err)
		}
	}
	printOutputAndExit(response.Result.(string))
}

func handleDropAllPort(invokePort string, servicePort string, channel *exec.LocalChannel, ctx context.Context) {
	if invokePort == "" && servicePort == "" {
		response := channel.Run(ctx, "iptables",
			fmt.Sprintf(`-A INPUT -p tcp --dport 22 -j ACCEPT`))
		if !response.Success {
			stopDropNet(servicePort, invokePort)
			printErrAndExit(response.Err)
		}
		response = channel.Run(ctx, "iptables",
			fmt.Sprintf(`-A INPUT -p udp --dport 22 -j ACCEPT`))
		if !response.Success {
			stopDropNet(servicePort, invokePort)
			printErrAndExit(response.Err)
		}
		response = channel.Run(ctx, "iptables",
			fmt.Sprintf(`-P INPUT DROP`))
		if !response.Success {
			stopDropNet(servicePort, invokePort)
			printErrAndExit(response.Err)
		}
		response = channel.Run(ctx, "iptables", fmt.Sprintf(`-P OUTPUT DROP`))
		if !response.Success {
			stopDropNet(servicePort, invokePort)
			printErrAndExit(response.Err)
		}
		printOutputAndExit(response.Result.(string))
	}
}

func stopDropNet(servicePort, invokePort string) {
	channel := exec.NewLocalChannel()
	ctx := context.Background()
	if invokePort == "" && servicePort == "" {
		channel.Run(ctx, "iptables", fmt.Sprintf(`-D INPUT -p tcp -m multiport --dports 22 -j ACCEPT`))
		channel.Run(ctx, "iptables", fmt.Sprintf(`-D INPUT -p udp -m multiport --dports 22 -j ACCEPT`))
		channel.Run(ctx, "iptables", fmt.Sprintf(`-P INPUT ACCEPT`))
		channel.Run(ctx, "iptables", fmt.Sprintf(`-P OUTPUT ACCEPT`))
	}
	if servicePort != "" {
		channel.Run(ctx, "iptables", fmt.Sprintf(`-D INPUT -p tcp --dport %s -j DROP`, servicePort))
		channel.Run(ctx, "iptables", fmt.Sprintf(`-D INPUT -p tcp --dport %s -j DROP`, servicePort))
	}
	if invokePort != "" {
		channel.Run(ctx, "iptables", fmt.Sprintf(`-D OUTPUT -p tcp --dport %s -j DROP`, servicePort))
		channel.Run(ctx, "iptables", fmt.Sprintf(`-D OUTPUT -p tcp --dport %s -j DROP`, servicePort))
	}
}
