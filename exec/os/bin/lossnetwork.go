package main

import (
	"fmt"
	"flag"
	"github.com/chaosblade-io/chaosblade/exec"
	"context"
	"github.com/chaosblade-io/chaosblade/transport"
)

var lossNetDevice, lossNetPercent, lossNetLocalPort, lossNetRemotePort, lossNetExcludePort string
var lossNetStart, lossNetStop bool

func main() {
	flag.StringVar(&lossNetDevice, "device", "", "network device")
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
		startLossNet(lossNetDevice, lossNetPercent, lossNetLocalPort, lossNetRemotePort, lossNetExcludePort)
	} else if lossNetStop {
		stopLossNet(lossNetDevice)
	} else {
		printErrAndExit("less --start or --stop flag")
	}
}

func startLossNet(device, percent, localPort, remotePort, excludePort string) {
	// invoke stop
	stopLossNet(device)
	channel := exec.NewLocalChannel()
	ctx := context.Background()

	if localPort == "" && remotePort == "" && excludePort == "" {
		response := channel.Run(ctx, "tc", fmt.Sprintf(`qdisc add dev %s root netem loss %s%%`, device, percent))
		if !response.Success {
			printErrAndExit(response.Err)
		}
		printOutputAndExit(response.Result.(string))
		return
	}
	response := addQdiscForLoss(channel, ctx, device, percent)
	if localPort == "" && remotePort == "" && excludePort != "" {
		response = addExcludePortFilterForLoss(excludePort, device, response, channel, ctx)
		printOutputAndExit(response.Result.(string))
		return
	}
	response = addLocalOrRemotePortFilterForLoss(localPort, response, channel, ctx, device, remotePort)
	printOutputAndExit(response.Result.(string))
}

// addLocalOrRemotePortFilterForLoss
func addLocalOrRemotePortFilterForLoss(localPort string, response *transport.Response, channel *exec.LocalChannel, ctx context.Context, device string, remotePort string) *transport.Response {
	if localPort != "" {
		response = channel.Run(ctx, "tc",
			fmt.Sprintf(`filter add dev %s parent 1: protocol ip prio 4 basic match "cmp(u16 at 0 layer transport eq %s)" flowid 1:4`, device, localPort))
		if !response.Success {
			stopLossNet(device)
			printErrAndExit(response.Err)
		}
	}
	if remotePort != "" {
		response = channel.Run(ctx, "tc",
			fmt.Sprintf(`filter add dev %s parent 1: protocol ip prio 4 basic match "cmp(u16 at 2 layer transport eq %s)" flowid 1:4`, device, remotePort))
		if !response.Success {
			stopLossNet(device)
			printErrAndExit(response.Err)
		}
	}
	return response
}

// addExcludePortFilterForLoss
func addExcludePortFilterForLoss(excludePort string, device string, response *transport.Response, channel *exec.LocalChannel, ctx context.Context) *transport.Response {
	response = channel.Run(ctx, "tc",
		fmt.Sprintf(
			`filter add dev %s parent 1: protocol ip prio 4 basic match "cmp(u16 at 0 layer transport gt 0) and cmp(u16 at 0 layer transport lt %s)" flowid 1:4`,
			device, excludePort))
	if !response.Success {
		stopLossNet(device)
		printErrAndExit(response.Err)
	}
	response = channel.Run(ctx, "tc",
		fmt.Sprintf(
			`filter add dev %s parent 1: protocol ip prio 4 basic match "cmp(u16 at 0 layer transport gt %s) and cmp(u16 at 0 layer transport lt 65535)" flowid 1:4`,
			device, excludePort))
	if !response.Success {
		stopLossNet(device)
		printErrAndExit(response.Err)
	}
	return response
}

// addQdiscForLoss
func addQdiscForLoss(channel *exec.LocalChannel, ctx context.Context, device string, percent string) *transport.Response {
	// invoke tc qdisc add dev ${networkPort} root handle 1: prio bands 4
	response := channel.Run(ctx, "tc", fmt.Sprintf(`qdisc add dev %s root handle 1: prio bands 4`, device))
	if !response.Success {
		// invoke stop
		stopLossNet(device)
		printErrAndExit(response.Err)
	}
	response = channel.Run(ctx, "tc", fmt.Sprintf(`qdisc add dev %s parent 1:4 handle 40: netem loss %s%%`, device, percent))
	if !response.Success {
		// invoke stop
		stopLossNet(device)
		printErrAndExit(response.Err)
	}
	return response
}

func stopLossNet(device string) {
	channel := exec.NewLocalChannel()
	ctx := context.Background()
	channel.Run(ctx, "tc", fmt.Sprintf(`filter del dev %s parent 1: prio 4 basic`, device))
	channel.Run(ctx, "tc", fmt.Sprintf(`qdisc del dev %s root`, device))
}
