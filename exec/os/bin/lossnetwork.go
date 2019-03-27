package main

import (
	"fmt"
	"flag"
	"github.com/chaosblade-io/chaosblade/exec"
	"context"
	"github.com/chaosblade-io/chaosblade/transport"
)

var lossNetDevice, lossNetPercent, lossNetServicePort, lossNetInvokePort, lossNetExcludePort string
var lossNetStart, lossNetStop bool

func main() {
	flag.StringVar(&lossNetDevice, "device", "", "network device")
	flag.StringVar(&lossNetPercent, "percent", "", "loss percent")
	flag.StringVar(&lossNetServicePort, "service-port", "", "service port")
	flag.StringVar(&lossNetInvokePort, "invoke-port", "", "invoke port")
	flag.StringVar(&lossNetExcludePort, "exclude-port", "", "exclude port")
	flag.BoolVar(&lossNetStart, "start", false, "start loss network")
	flag.BoolVar(&lossNetStop, "stop", false, "stop loss network")
	flag.Parse()

	if lossNetStart == lossNetStop {
		printErrAndExit("must add --start or --stop flag")
	}
	if lossNetStart {
		startLossNet(lossNetDevice, lossNetPercent, lossNetServicePort, lossNetInvokePort, lossNetExcludePort)
	} else if lossNetStop {
		stopLossNet(lossNetDevice)
	} else {
		printErrAndExit("less --start or --stop flag")
	}
}

func startLossNet(device, percent, servicePort, invokePort, excludePort string) {
	// invoke stop
	stopLossNet(device)
	channel := exec.NewLocalChannel()
	ctx := context.Background()

	if servicePort == "" && invokePort == "" && excludePort == "" {
		response := channel.Run(ctx, "tc", fmt.Sprintf(`qdisc add dev %s root netem loss %s%%`, device, percent))
		if !response.Success {
			printErrAndExit(response.Err)
		}
		printOutputAndExit(response.Result.(string))
		return
	}
	response := addQdiscForLoss(channel, ctx, device, percent)
	if servicePort == "" && invokePort == "" && excludePort != "" {
		response = addExcludePortFilterForLoss(excludePort, device, response, channel, ctx)
		printOutputAndExit(response.Result.(string))
		return
	}
	response = addServiceOrInvokePortFilterForLoss(servicePort, response, channel, ctx, device, invokePort)
	printOutputAndExit(response.Result.(string))
}

// addServiceOrInvokePortFilterForLoss
func addServiceOrInvokePortFilterForLoss(servicePort string, response *transport.Response, channel *exec.LocalChannel, ctx context.Context, device string, invokePort string) *transport.Response {
	if servicePort != "" {
		response = channel.Run(ctx, "tc",
			fmt.Sprintf(`filter add dev %s parent 1: protocol ip prio 4 basic match "cmp(u16 at 0 layer transport eq %s)" flowid 1:4`, device, servicePort))
		if !response.Success {
			stopLossNet(device)
			printErrAndExit(response.Err)
		}
	}
	if invokePort != "" {
		response = channel.Run(ctx, "tc",
			fmt.Sprintf(`filter add dev %s parent 1: protocol ip prio 4 basic match "cmp(u16 at 2 layer transport eq %s)" flowid 1:4`, device, invokePort))
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
