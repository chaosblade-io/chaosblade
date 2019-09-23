package main

import (
	"context"
	"flag"
	"fmt"

	"github.com/chaosblade-io/chaosblade/exec"
	"github.com/chaosblade-io/chaosblade/exec/os/bin"
	"github.com/chaosblade-io/chaosblade/transport"
	"strings"
	"github.com/chaosblade-io/chaosblade/util"
)

var dlNetInterface, dlLocalPort, dlRemotePort, dlExcludePort string
var dlDestinationIp string
var lossNetPercent, delayNetTime, delayNetOffset string
var dlNetStart, dlNetStop bool

const delimiter = ","

func main() {
	flag.StringVar(&dlNetInterface, "interface", "", "network interface")
	flag.StringVar(&delayNetTime, "time", "", "delay time")
	flag.StringVar(&delayNetOffset, "offset", "", "delay offset")
	flag.StringVar(&lossNetPercent, "percent", "", "loss percent")
	flag.StringVar(&dlLocalPort, "local-port", "", "local ports, for example: 80,8080,8081")
	flag.StringVar(&dlRemotePort, "remote-port", "", "remote ports, for example: 80,8080,8081")
	flag.StringVar(&dlExcludePort, "exclude-port", "", "exclude ports, for example: 22,23")
	flag.StringVar(&dlDestinationIp, "destination-ip", "", "destination ip")
	flag.BoolVar(&dlNetStart, "start", false, "start delay")
	flag.BoolVar(&dlNetStop, "stop", false, "stop delay")
	util.AddDebugFlag()
	flag.Parse()
	util.InitLog(util.Bin)
	if dlNetInterface == "" {
		bin.PrintErrAndExit("less --interface flag")
	}

	if dlNetStart {
		var classRule string
		if lossNetPercent != "" {
			classRule = fmt.Sprintf("netem loss %s%%", lossNetPercent)
		} else if delayNetTime != "" {
			classRule = fmt.Sprintf("netem delay %sms %sms", delayNetTime, delayNetOffset)
		}
		startNet(dlNetInterface, classRule, dlLocalPort, dlRemotePort, dlExcludePort, dlDestinationIp)
	} else if dlNetStop {
		stopNet(dlNetInterface)
	} else {
		bin.PrintErrAndExit("less --start or --stop flag")
	}
}

var channel = exec.NewLocalChannel()

func startNet(netInterface, classRule, localPort, remotePort, excludePort, destIp string) {
	ctx := context.Background()
	// assert localPort and remotePort
	if localPort == "" && remotePort == "" && excludePort == "" && destIp == "" {
		response := channel.Run(ctx, "tc", fmt.Sprintf(`qdisc add dev %s root %s`, netInterface, classRule))
		if !response.Success {
			bin.PrintErrAndExit(response.Err)
		}
		bin.PrintOutputAndExit(response.Result.(string))
		return
	}
	response := addQdiscForDL(channel, ctx, netInterface)
	// only ip
	if localPort == "" && remotePort == "" && excludePort == "" {
		response = addIpFilterForDL(ctx, channel, netInterface, classRule, destIp)
		bin.PrintOutputAndExit(response.Result.(string))
		return
	}
	ipRule := getIpRule(destIp)
	// exclude
	if localPort == "" && remotePort == "" && excludePort != "" {
		response = addExcludePortFilterForDL(ctx, channel, netInterface, classRule, excludePort, ipRule)
		bin.PrintOutputAndExit(response.Result.(string))
		return
	}
	// local port or remote port
	response = addLocalOrRemotePortForDL(ctx, channel, netInterface, classRule, localPort, remotePort, ipRule)
	bin.PrintOutputAndExit(response.Result.(string))
}

func getIpRule(destIp string) string {
	if destIp == "" {
		return ""
	}
	return fmt.Sprintf("match ip dst %s", destIp)
}

// addIpFilterForDL
func addIpFilterForDL(ctx context.Context, channel exec.Channel, netInterface string, classRule, destIp string) *transport.Response {
	response := channel.Run(ctx, "tc",
		fmt.Sprintf(`qdisc add dev %s parent 1:4 handle 40: %s`, netInterface, classRule))
	if !response.Success {
		stopNet(netInterface)
		bin.PrintErrAndExit(response.Err)
		return response
	}
	args := fmt.Sprintf(
		`filter add dev %s parent 1: prio 4 protocol ip u32 match ip dst %s flowid 1:4`,
		netInterface, destIp)
	response = channel.Run(ctx, "tc", args)
	if !response.Success {
		stopDLNetFunc(netInterface)
		bin.PrintErrAndExit(response.Err)
	}
	return response
}

var stopDLNetFunc = stopNet
// addLocalOrRemotePortForDL creates class rule in 1:4 queue and add filter to the queue
func addLocalOrRemotePortForDL(ctx context.Context, channel exec.Channel,
	netInterface, classRule, localPort, remotePort, ipRule string) *transport.Response {
	response := channel.Run(ctx, "tc",
		fmt.Sprintf(`qdisc add dev %s parent 1:4 handle 40: %s`, netInterface, classRule))
	if !response.Success {
		stopNet(netInterface)
		bin.PrintErrAndExit(response.Err)
		return response
	}
	// local port 0
	if localPort != "" {
		ports := strings.Split(localPort, delimiter)
		args := fmt.Sprintf(
			`filter add dev %s parent 1: prio 4 protocol ip u32 %s match ip sport %s 0xffff flowid 1:4`,
			netInterface, ipRule, ports[0])
		if len(ports) > 1 {
			for i := 1; i < len(ports); i++ {
				args = fmt.Sprintf(
					`%s && \
					tc filter add dev %s parent 1: prio 4 protocol ip u32 %s match ip sport %s 0xffff flowid 1:4`,
					args, netInterface, ipRule, ports[i])
			}
		}
		response = channel.Run(ctx, "tc", args)
		if !response.Success {
			stopDLNetFunc(netInterface)
			bin.PrintErrAndExit(response.Err)
		}
	}
	// remote port 2
	if remotePort != "" {
		ports := strings.Split(remotePort, delimiter)
		args := fmt.Sprintf(
			`filter add dev %s parent 1: prio 4 protocol ip u32 %s match ip dport %s 0xffff flowid 1:4`,
			netInterface, ipRule, ports[0])
		if len(ports) > 1 {
			for i := 1; i < len(ports); i++ {
				args = fmt.Sprintf(
					`%s && \
					tc filter add dev %s parent 1: prio 4 protocol ip u32 %s match ip dport %s 0xffff flowid 1:4`,
					args, netInterface, ipRule, ports[i])
			}
		}
		response = channel.Run(ctx, "tc", args)
		if !response.Success {
			stopDLNetFunc(netInterface)
			bin.PrintErrAndExit(response.Err)
		}
	}
	return response
}

// addExcludePortFilterForDL create class rule for each band and add filter to 1:4
func addExcludePortFilterForDL(ctx context.Context, channel exec.Channel,
	netInterface, classRule, excludePort, ipRule string) *transport.Response {
	args := fmt.Sprintf(
		`qdisc add dev %s parent 1:1 %s && \
			tc qdisc add dev %s parent 1:2 %s && \
			tc qdisc add dev %s parent 1:3 %s && \
			tc qdisc add dev %s parent 1:4 handle 40: pfifo_fast`,
		netInterface, classRule, netInterface, classRule, netInterface, classRule, netInterface)
	ports := strings.Split(excludePort, delimiter)

	for idx := range ports {
		args = fmt.Sprintf(
			`%s && \
			tc filter add dev %s parent 1: prio 4 protocol ip u32 %s match ip sport %s 0xffff flowid 1:4 && \
			tc filter add dev %s parent 1: prio 4 protocol ip u32 %s match ip dport %s 0xffff flowid 1:4`,
			args, netInterface, ipRule, ports[idx], netInterface, ipRule, ports[idx])
	}

	response := channel.Run(ctx, "tc", args)
	if !response.Success {
		stopDLNetFunc(netInterface)
		bin.PrintErrAndExit(response.Err)
		return response
	}
	return response
}

// addQdiscForDL creates bands for filter
func addQdiscForDL(channel exec.Channel, ctx context.Context, netInterface string) *transport.Response {
	// add tc filter for delay specify port
	response := channel.Run(ctx, "tc", fmt.Sprintf(`qdisc add dev %s root handle 1: prio bands 4`, netInterface))
	if !response.Success {
		bin.PrintErrAndExit(response.Err)
		return response
	}
	return response
}

// stopNet, no need to add os.Exit
func stopNet(netInterface string) {
	ctx := context.Background()
	channel.Run(ctx, "tc", fmt.Sprintf(`filter del dev %s parent 1: prio 4`, netInterface))
	channel.Run(ctx, "tc", fmt.Sprintf(`qdisc del dev %s root`, netInterface))
}
