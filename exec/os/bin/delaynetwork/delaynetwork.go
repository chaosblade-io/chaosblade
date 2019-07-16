package main

import (
	"context"
	"flag"
	"fmt"

	"github.com/chaosblade-io/chaosblade/exec"
	"github.com/chaosblade-io/chaosblade/exec/os/bin"
	"github.com/chaosblade-io/chaosblade/transport"
)

var delayNetInterface, delayNetTime, delayNetOffset, delayLocalPort, delayRemotePort, delayExcludePort string
var delayNetStart, delayNetStop bool

func main() {
	flag.StringVar(&delayNetInterface, "interface", "", "network interface")
	flag.StringVar(&delayNetTime, "time", "", "delay time")
	flag.StringVar(&delayNetOffset, "offset", "", "delay offset")
	flag.StringVar(&delayLocalPort, "local-port", "", "local port")
	flag.StringVar(&delayRemotePort, "remote-port", "", "remote port")
	flag.StringVar(&delayExcludePort, "exclude-port", "", "exclude port")
	flag.BoolVar(&delayNetStart, "start", false, "start delay")
	flag.BoolVar(&delayNetStop, "stop", false, "stop delay")
	flag.Parse()

	if delayNetInterface == "" {
		bin.PrintErrAndExit("less --interface flag")
	}

	if delayNetStart {
		startDelayNet(delayNetInterface, delayNetTime, delayNetOffset, delayLocalPort, delayRemotePort, delayExcludePort)
	} else if delayNetStop {
		stopDelayNet(delayNetInterface)
	} else {
		bin.PrintErrAndExit("less --start or --stop flag")
	}
}

var channel = exec.NewLocalChannel()
func startDelayNet(netInterface, time, offset, localPort, remotePort, excludePort string) {
	ctx := context.Background()
	// assert localPort and remotePort
	if localPort == "" && remotePort == "" && excludePort == "" {
		response := channel.Run(ctx, "tc", fmt.Sprintf(`qdisc add dev %s root netem delay %sms %sms`, netInterface, time, offset))
		if !response.Success {
			bin.PrintErrAndExit(response.Err)
		}
		bin.PrintOutputAndExit(response.Result.(string))
		return
	}
	response := addQdiscForDelay(channel, ctx, netInterface, time, offset)
	if localPort == "" && remotePort == "" && excludePort != "" {
		response = addExcludePortFilterForDelay(excludePort, netInterface, response, channel, ctx)
		bin.PrintOutputAndExit(response.Result.(string))
		return
	}
	response = addLocalOrRemotePortForDelay(localPort, response, channel, ctx, netInterface, remotePort)
	bin.PrintOutputAndExit(response.Result.(string))
}

var stopDelayNetFunc = stopDelayNet
// addLocalOrRemotePortForDelay
func addLocalOrRemotePortForDelay(localPort string, response *transport.Response, channel exec.Channel, ctx context.Context, netInterface string, remotePort string) *transport.Response {
	// local port 0
	if localPort != "" {
		response = channel.Run(ctx, "tc",
			fmt.Sprintf(`filter add dev %s parent 1: protocol ip prio 4 basic match "cmp(u16 at 0 layer transport eq %s)" flowid 1:4`, netInterface, localPort))
		if !response.Success {
			stopDelayNetFunc(netInterface)
			bin.PrintErrAndExit(response.Err)
		}
	}
	// remote port 2
	if remotePort != "" {
		response = channel.Run(ctx, "tc",
			fmt.Sprintf(`filter add dev %s parent 1: protocol ip prio 4 basic match "cmp(u16 at 2 layer transport eq %s)" flowid 1:4`, netInterface, remotePort))
		if !response.Success {
			stopDelayNetFunc(netInterface)
			bin.PrintErrAndExit(response.Err)
		}
	}
	return response
}

// addExcludePortFilterForDelay
func addExcludePortFilterForDelay(excludePort string, netInterface string, response *transport.Response, channel exec.Channel, ctx context.Context) *transport.Response {
	response = channel.Run(ctx, "tc",
		fmt.Sprintf(
			`filter add dev %s parent 1: protocol ip prio 4 basic match "cmp(u16 at 0 layer transport gt 0) and cmp(u16 at 0 layer transport lt %s)" flowid 1:4`,
			netInterface, excludePort))
	if !response.Success {
		stopDelayNetFunc(netInterface)
		bin.PrintErrAndExit(response.Err)
		return response
	}
	response = channel.Run(ctx, "tc",
		fmt.Sprintf(
			`filter add dev %s parent 1: protocol ip prio 4 basic match "cmp(u16 at 0 layer transport gt %s) and cmp(u16 at 0 layer transport lt 65535)" flowid 1:4`,
			netInterface, excludePort))
	if !response.Success {
		stopDelayNetFunc(netInterface)
		bin.PrintErrAndExit(response.Err)
		return response
	}
	return response
}

// addQdiscForDelay
func addQdiscForDelay(channel exec.Channel, ctx context.Context, netInterface string, time string, offset string) *transport.Response {
	// add tc filter for delay specify port
	response := channel.Run(ctx, "tc", fmt.Sprintf(`qdisc add dev %s root handle 1: prio bands 4`, netInterface))
	if !response.Success {
		bin.PrintErrAndExit(response.Err)
		return response
	}
	response = channel.Run(ctx, "tc",
		fmt.Sprintf(`qdisc add dev %s parent 1:4 handle 40: netem delay %sms %sms`, netInterface, time, offset))
	if !response.Success {
		stopDelayNet(netInterface)
		bin.PrintErrAndExit(response.Err)
		return response
	}
	return response
}

// stopDelayNet, no need to add os.Exit
func stopDelayNet(netInterface string) {
	ctx := context.Background()
	channel.Run(ctx, "tc", fmt.Sprintf(`filter del dev %s parent 1: prio 4 basic`, netInterface))
	channel.Run(ctx, "tc", fmt.Sprintf(`qdisc del dev %s root`, netInterface))
}
