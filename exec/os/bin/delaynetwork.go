package main

import (
	"fmt"
	"flag"
	"github.com/chaosblade-io/chaosblade/exec"
	"context"
	"github.com/chaosblade-io/chaosblade/transport"
)

var delayNetDevice, delayNetTime, delayNetOffset, delayServicePort, delayInvokePort, delayExcludePort string
var delayNetStart, delayNetStop bool

func main() {
	flag.StringVar(&delayNetDevice, "device", "", "network device")
	flag.StringVar(&delayNetTime, "time", "", "delay time")
	flag.StringVar(&delayNetOffset, "offset", "", "delay offset")
	flag.StringVar(&delayServicePort, "service-port", "", "service port")
	flag.StringVar(&delayInvokePort, "invoke-port", "", "invoke port")
	flag.StringVar(&delayExcludePort, "exclude-port", "", "exclude port")
	flag.BoolVar(&delayNetStart, "start", false, "start delay")
	flag.BoolVar(&delayNetStop, "stop", false, "stop delay")
	flag.Parse()

	if delayNetDevice == "" {
		printErrAndExit("less device arg")
	}

	if delayNetStart {
		startDelayNet(delayNetDevice, delayNetTime, delayNetOffset, delayServicePort, delayInvokePort, delayExcludePort)
	} else if delayNetStop {
		stopDelayNet(delayNetDevice)
	} else {
		printErrAndExit("less --start or --stop flag")
	}
}

func startDelayNet(device, time, offset, servicePort, invokePort, excludePort string) {
	channel := exec.NewLocalChannel()
	ctx := context.Background()
	// assert servicePort and invokePort
	if servicePort == "" && invokePort == "" && excludePort == "" {
		response := channel.Run(ctx, "tc", fmt.Sprintf(`qdisc add dev %s root netem delay %sms %sms`, device, time, offset))
		if !response.Success {
			printErrAndExit(response.Err)
		}
		printOutputAndExit(response.Result.(string))
		return
	}
	response := addQdiscForDelay(channel, ctx, device, time, offset)
	if servicePort == "" && invokePort == "" && excludePort != "" {
		response = addExcludePortFilterForDelay(excludePort, device, response, channel, ctx)
		printOutputAndExit(response.Result.(string))
		return
	}
	response = addServiceOrInvokePortForDelay(servicePort, response, channel, ctx, device, invokePort)
	printOutputAndExit(response.Result.(string))
}

// addServiceOrInvokePortForDelay
func addServiceOrInvokePortForDelay(servicePort string, response *transport.Response, channel *exec.LocalChannel, ctx context.Context, device string, invokePort string) *transport.Response {
	// service port 0
	if servicePort != "" {
		response = channel.Run(ctx, "tc",
			fmt.Sprintf(`filter add dev %s parent 1: protocol ip prio 4 basic match "cmp(u16 at 0 layer transport eq %s)" flowid 1:4`, device, servicePort))
		if !response.Success {
			stopDelayNet(device)
			printErrAndExit(response.Err)
		}
	}
	// invoke port 2
	if invokePort != "" {
		response = channel.Run(ctx, "tc",
			fmt.Sprintf(`filter add dev %s parent 1: protocol ip prio 4 basic match "cmp(u16 at 2 layer transport eq %s)" flowid 1:4`, device, invokePort))
		if !response.Success {
			stopDelayNet(device)
			printErrAndExit(response.Err)
		}
	}
	return response
}

// addExcludePortFilterForDelay
func addExcludePortFilterForDelay(excludePort string, device string, response *transport.Response, channel *exec.LocalChannel, ctx context.Context) *transport.Response {
	response = channel.Run(ctx, "tc",
		fmt.Sprintf(
			`filter add dev %s parent 1: protocol ip prio 4 basic match "cmp(u16 at 0 layer transport gt 0) and cmp(u16 at 0 layer transport lt %s)" flowid 1:4`,
			device, excludePort))
	if !response.Success {
		stopDelayNet(device)
		printErrAndExit(response.Err)
	}
	response = channel.Run(ctx, "tc",
		fmt.Sprintf(
			`filter add dev %s parent 1: protocol ip prio 4 basic match "cmp(u16 at 0 layer transport gt %s) and cmp(u16 at 0 layer transport lt 65535)" flowid 1:4`,
			device, excludePort))
	if !response.Success {
		stopDelayNet(device)
		printErrAndExit(response.Err)
	}
	return response
}

// addQdiscForDelay
func addQdiscForDelay(channel *exec.LocalChannel, ctx context.Context, device string, time string, offset string) *transport.Response {
	// add tc filter for delay specify port
	response := channel.Run(ctx, "tc", fmt.Sprintf(`qdisc add dev %s root handle 1: prio bands 4`, device))
	if !response.Success {
		printErrAndExit(response.Err)
	}
	response = channel.Run(ctx, "tc",
		fmt.Sprintf(`qdisc add dev %s parent 1:4 handle 40: netem delay %sms %sms`, device, time, offset))
	if !response.Success {
		stopDelayNet(device)
		printErrAndExit(response.Err)
	}
	return response
}

// stopDelayNet, no need to add os.Exit
func stopDelayNet(device string) {
	channel := exec.NewLocalChannel()
	ctx := context.Background()
	channel.Run(ctx, "tc", fmt.Sprintf(`filter del dev %s parent 1: prio 4 basic`, device))
	channel.Run(ctx, "tc", fmt.Sprintf(`qdisc del dev %s root`, device))
}
