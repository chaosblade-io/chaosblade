package main

import (
	"fmt"
	"flag"
	"github.com/chaosblade-io/chaosblade/exec"
	"context"
)

var lossNetDevice, lossNetPercent, lossNetServicePort string
var lossNetStart, lossNetStop bool

func main() {
	flag.StringVar(&lossNetDevice, "device", "", "network device")
	flag.StringVar(&lossNetPercent, "percent", "", "loss percent")
	flag.StringVar(&lossNetServicePort, "service-port", "", "service port")
	flag.BoolVar(&lossNetStart, "start", false, "start loss network")
	flag.BoolVar(&lossNetStop, "stop", false, "stop loss network")
	flag.Parse()

	if lossNetStart == lossNetStop {
		printErrAndExit("must add --start or --stop flag")
	}
	if lossNetStart {
		startLossNet(lossNetDevice, lossNetPercent, lossNetServicePort)
	} else if lossNetStop {
		stopLossNet(lossNetDevice)
	} else {
		printErrAndExit("less --start or --stop flag")
	}
}

func startLossNet(device, percent, servicePort string) {
	// invoke stop
	stopLossNet(device)
	channel := exec.NewLocalChannel()
	ctx := context.Background()

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
	if servicePort == "" {
		// add loss
		if !response.Success {
			stopLossNet(device)
			printErrAndExit(response.Err)
		}
		// filter >32
		response = channel.Run(ctx, "tc",
			fmt.Sprintf(`filter add dev %s parent 1: protocol ip prio 4 basic match "cmp(u16 at 0 layer transport gt 32) and cmp(u16 at 0 layer transport lt 8000)" flowid 1:4`, device))
		if !response.Success {
			stopLossNet(device)
			printErrAndExit(response.Err)
		}
		// !=8000
		response = channel.Run(ctx, "tc",
			fmt.Sprintf(`filter add dev %s parent 1: protocol ip prio 4 basic match "cmp(u16 at 0 layer transport gt 8000) and cmp(u16 at 0 layer transport lt 9527)" flowid 1:4`, device))
		if !response.Success {
			stopLossNet(device)
			printErrAndExit(response.Err)
		}
		// !=9527
		response = channel.Run(ctx, "tc",
			fmt.Sprintf(`filter add dev %s parent 1: protocol ip prio 4 basic match "cmp(u16 at 0 layer transport gt 9527) and cmp(u16 at 0 layer transport lt 65535)" flowid 1:4`, device))
		if !response.Success {
			stopLossNet(device)
			printErrAndExit(response.Err)
		}
		printOutputAndExit(response.Result.(string))
	} else {
		response = channel.Run(ctx, "tc",
			fmt.Sprintf(`filter add dev %s parent 1: protocol ip prio 4 basic match "cmp(u16 at 0 layer transport eq %s)" flowid 1:4`, device, servicePort))
		if !response.Success {
			stopLossNet(device)
			printErrAndExit(response.Err)
		}
		printOutputAndExit(response.Result.(string))
	}
}

func stopLossNet(device string) {
	exec.NewLocalChannel().Run(context.Background(), "tc", fmt.Sprintf(`qdisc del dev %s root`, device))
}
