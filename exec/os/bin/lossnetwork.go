package main

import (
	"fmt"
	"flag"
	"github.com/chaosblade-io/chaosblade/exec"
	"context"
	"github.com/chaosblade-io/chaosblade/transport"
)

var lossNetInterface, lossNetPercent, lossNetLocalPort, lossNetRemotePort, lossNetExcludePort string
var lossNetStart, lossNetStop bool

func main() {
	flag.StringVar(&lossNetInterface, "interface", "", "network interface")
	flag.StringVar(&lossNetPercent, "percent", "", "loss percent")
	flag.StringVar(&lossNetLocalPort, "local-port", "", "local port")
	flag.StringVar(&lossNetRemotePort, "remote-port", "", "remote port")
	flag.StringVar(&lossNetExcludePort, "exclude-port", "", "exclude port")
	flag.BoolVar(&lossNetStart, "start", false, "start loss network")
	flag.BoolVar(&lossNetStop, "stop", false, "stop loss network")
	flag.Parse()

	if lossNetStart == lossNetStop {
		printErrAndExit("must add --start or --stop flag")
	}
	if lossNetStart {
		startLossNet(lossNetInterface, lossNetPercent, lossNetLocalPort, lossNetRemotePort, lossNetExcludePort)
	} else if lossNetStop {
		stopLossNet(lossNetInterface)
	} else {
		printErrAndExit("less --start or --stop flag")
	}
}

func startLossNet(netInterface, percent, localPort, remotePort, excludePort string) {
	// invoke stop
	stopLossNet(netInterface)
	channel := exec.NewLocalChannel()
	ctx := context.Background()

	if localPort == "" && remotePort == "" && excludePort == "" {
		response := channel.Run(ctx, "tc", fmt.Sprintf(`qdisc add dev %s root netem loss %s%%`, netInterface, percent))
		if !response.Success {
			printErrAndExit(response.Err)
		}
		printOutputAndExit(response.Result.(string))
		return
	}
	response := addQdiscForLoss(channel, ctx, netInterface, percent)
	if localPort == "" && remotePort == "" && excludePort != "" {
		response = addExcludePortFilterForLoss(excludePort, netInterface, response, channel, ctx)
		printOutputAndExit(response.Result.(string))
		return
	}
	response = addLocalOrRemotePortFilterForLoss(localPort, response, channel, ctx, netInterface, remotePort)
	printOutputAndExit(response.Result.(string))
}

// addLocalOrRemotePortFilterForLoss
func addLocalOrRemotePortFilterForLoss(localPort string, response *transport.Response, channel *exec.LocalChannel, ctx context.Context, netInterface string, remotePort string) *transport.Response {
	if localPort != "" {
		response = channel.Run(ctx, "tc",
			fmt.Sprintf(`filter add dev %s parent 1: protocol ip prio 4 basic match "cmp(u16 at 0 layer transport eq %s)" flowid 1:4`, netInterface, localPort))
		if !response.Success {
			stopLossNet(netInterface)
			printErrAndExit(response.Err)
		}
	}
	if remotePort != "" {
		response = channel.Run(ctx, "tc",
			fmt.Sprintf(`filter add dev %s parent 1: protocol ip prio 4 basic match "cmp(u16 at 2 layer transport eq %s)" flowid 1:4`, netInterface, remotePort))
		if !response.Success {
			stopLossNet(netInterface)
			printErrAndExit(response.Err)
		}
	}
	return response
}

// addExcludePortFilterForLoss
func addExcludePortFilterForLoss(excludePort string, netInterface string, response *transport.Response, channel *exec.LocalChannel, ctx context.Context) *transport.Response {
	response = channel.Run(ctx, "tc",
		fmt.Sprintf(
			`filter add dev %s parent 1: protocol ip prio 4 basic match "cmp(u16 at 0 layer transport gt 0) and cmp(u16 at 0 layer transport lt %s)" flowid 1:4`,
			netInterface, excludePort))
	if !response.Success {
		stopLossNet(netInterface)
		printErrAndExit(response.Err)
	}
	response = channel.Run(ctx, "tc",
		fmt.Sprintf(
			`filter add dev %s parent 1: protocol ip prio 4 basic match "cmp(u16 at 0 layer transport gt %s) and cmp(u16 at 0 layer transport lt 65535)" flowid 1:4`,
			netInterface, excludePort))
	if !response.Success {
		stopLossNet(netInterface)
		printErrAndExit(response.Err)
	}
	return response
}

// addQdiscForLoss
func addQdiscForLoss(channel *exec.LocalChannel, ctx context.Context, netInterface string, percent string) *transport.Response {
	// invoke tc qdisc add dev ${networkPort} root handle 1: prio bands 4
	response := channel.Run(ctx, "tc", fmt.Sprintf(`qdisc add dev %s root handle 1: prio bands 4`, netInterface))
	if !response.Success {
		// invoke stop
		stopLossNet(netInterface)
		printErrAndExit(response.Err)
	}
	response = channel.Run(ctx, "tc", fmt.Sprintf(`qdisc add dev %s parent 1:4 handle 40: netem loss %s%%`, netInterface, percent))
	if !response.Success {
		// invoke stop
		stopLossNet(netInterface)
		printErrAndExit(response.Err)
	}
	return response
}

func stopLossNet(netInterface string) {
	channel := exec.NewLocalChannel()
	ctx := context.Background()
	channel.Run(ctx, "tc", fmt.Sprintf(`filter del dev %s parent 1: prio 4 basic`, netInterface))
	channel.Run(ctx, "tc", fmt.Sprintf(`qdisc del dev %s root`, netInterface))
}
