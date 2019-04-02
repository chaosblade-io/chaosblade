package main

import (
	"fmt"
	"flag"
	"github.com/chaosblade-io/chaosblade/exec"
	"context"
	"github.com/chaosblade-io/chaosblade/transport"
)

var delayNetDevice, delayNetTime, delayNetOffset, delayLocalPort, delayRemotePort, delayExcludePort string
var delayNetStart, delayNetStop bool

func main() {
	flag.StringVar(&delayNetDevice, "device", "", "network device")
	flag.StringVar(&delayNetTime, "time", "", "delay time")
	flag.StringVar(&delayNetOffset, "offset", "", "delay offset")
	flag.StringVar(&delayLocalPort, "local-port", "", "local port")
	flag.StringVar(&delayRemotePort, "remote-port", "", "remote port")
	flag.StringVar(&delayExcludePort, "exclude-port", "", "exclude port")
	flag.BoolVar(&delayNetStart, "start", false, "start delay")
	flag.BoolVar(&delayNetStop, "stop", false, "stop delay")
	flag.Parse()

	if delayNetDevice == "" {
		printErrAndExit("less device arg")
	}

	if delayNetStart {
		startDelayNet(delayNetDevice, delayNetTime, delayNetOffset, delayLocalPort, delayRemotePort, delayExcludePort)
	} else if delayNetStop {
		stopDelayNet(delayNetDevice)
	} else {
		printErrAndExit("less --start or --stop flag")
	}
}

func startDelayNet(device, time, offset, localPort, remotePort, excludePort string) {
	channel := exec.NewLocalChannel()
	ctx := context.Background()
	// assert localPort and remotePort
	if localPort == "" && remotePort == "" && excludePort == "" {
		response := channel.Run(ctx, "tc", fmt.Sprintf(`qdisc add dev %s root netem delay %sms %sms`, device, time, offset))
		if !response.Success {
			printErrAndExit(response.Err)
		}
		printOutputAndExit(response.Result.(string))
		return
	}
	response := addQdiscForDelay(channel, ctx, device, time, offset)
	if localPort == "" && remotePort == "" && excludePort != "" {
		response = addExcludePortFilterForDelay(excludePort, device, response, channel, ctx)
		printOutputAndExit(response.Result.(string))
		return
	}
	response = addLocalOrRemotePortForDelay(localPort, response, channel, ctx, device, remotePort)
	printOutputAndExit(response.Result.(string))
}

// addLocalOrRemotePortForDelay
func addLocalOrRemotePortForDelay(localPort string, response *transport.Response, channel *exec.LocalChannel, ctx context.Context, device string, remotePort string) *transport.Response {
	// local port 0
	if localPort != "" {
		response = channel.Run(ctx, "tc",
			fmt.Sprintf(`filter add dev %s parent 1: protocol ip prio 4 basic match "cmp(u16 at 0 layer transport eq %s)" flowid 1:4`, device, localPort))
		if !response.Success {
			stopDelayNet(device)
			printErrAndExit(response.Err)
		}
	}
	// remote port 2
	if remotePort != "" {
		response = channel.Run(ctx, "tc",
			fmt.Sprintf(`filter add dev %s parent 1: protocol ip prio 4 basic match "cmp(u16 at 2 layer transport eq %s)" flowid 1:4`, device, remotePort))
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
