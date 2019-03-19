package main

import (
	"fmt"
	"flag"
	"github.com/chaosblade-io/chaosblade/exec"
	"context"
)

var delayNetDevice, delayNetTime, delayNetOffset, delayServicePort, delayInvokePort string
var delayNetStart, delayNetStop bool

func main() {
	flag.StringVar(&delayNetDevice, "device", "", "network device")
	flag.StringVar(&delayNetTime, "time", "", "delay time")
	flag.StringVar(&delayNetOffset, "offset", "", "delay offset")
	flag.StringVar(&delayServicePort, "service-port", "", "service port")
	flag.StringVar(&delayInvokePort, "invoke-port", "", "invoke port")
	flag.BoolVar(&delayNetStart, "start", false, "start delay")
	flag.BoolVar(&delayNetStop, "stop", false, "stop delay")
	flag.Parse()

	if delayNetDevice == "" {
		printErrAndExit("less device arg")
	}

	if delayNetStart {
		startDelayNet(delayNetDevice, delayNetTime, delayNetOffset, delayServicePort, delayInvokePort)
	} else if delayNetStop {
		stopDelayNet(delayNetDevice)
	} else {
		printErrAndExit("less --start or --stop flag")
	}
}

func startDelayNet(device, time, offset, servicePort, invokePort string) {
	channel := exec.NewLocalChannel()
	ctx := context.Background()
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
	// service port 0
	// invoke port 2
	if servicePort != "" {
		response = channel.Run(ctx, "tc",
			fmt.Sprintf(`filter add dev %s parent 1: protocol ip prio 4 basic match "cmp(u16 at %s layer transport eq %s)" flowid 1:4`, device, "0", servicePort))
		if !response.Success {
			stopDelayNet(device)
			printErrAndExit(response.Err)
		}
	}
	if invokePort != "" {
		response = channel.Run(ctx, "tc",
			fmt.Sprintf(`filter add dev %s parent 1: protocol ip prio 4 basic match "cmp(u16 at %s layer transport eq %s)" flowid 1:4`, device, "2", invokePort))
		if !response.Success {
			stopDelayNet(device)
			printErrAndExit(response.Err)
		}
	}
	printOutputAndExit(response.Result.(string))
}

// stopDelayNet, no need to add os.Exit
func stopDelayNet(device string) {
	channel := exec.NewLocalChannel()
	ctx := context.Background()
	channel.Run(ctx, "tc", fmt.Sprintf(`filter del dev %s parent 1: prio 4 basic`, device))
	channel.Run(ctx, "tc", fmt.Sprintf(`qdisc del dev %s root`, device))
}
