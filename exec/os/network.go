package os

import (
	"github.com/chaosblade-io/chaosblade/exec"
	"fmt"
	"github.com/chaosblade-io/chaosblade/util"
	"strings"
)

type NetworkCommandSpec struct {
}

func (*NetworkCommandSpec) Name() string {
	return "network"
}

func (*NetworkCommandSpec) ShortDesc() string {
	return "Network experiment"
}

func (*NetworkCommandSpec) LongDesc() string {
	return "Network experiment"
}

func (*NetworkCommandSpec) Example() string {
	return `network delay --interface eth0 --time 3000

# You can execute "blade query network interface" command to query the interfaces`
}

func (*NetworkCommandSpec) Actions() []exec.ExpActionCommandSpec {
	return []exec.ExpActionCommandSpec{
		&DelayActionSpec{},
		&DropActionSpec{},
		&DnsActionSpec{},
		&LossActionSpec{},
	}
}

func (*NetworkCommandSpec) Flags() []exec.ExpFlagSpec {
	return []exec.ExpFlagSpec{}
}

func (*NetworkCommandSpec) PreExecutor() exec.PreExecutor {
	return nil
}

// dlNetworkBin for delay and loss experiments
var dlNetworkBin = "chaos_dlnetwork"

var commFlags = []exec.ExpFlagSpec{
	&exec.ExpFlag{
		Name: "local-port",
		Desc: "Ports for local service. Support for configuring multiple ports, separated by commas or connector representing ranges, for example: 80,8000-8080",
	},
	&exec.ExpFlag{
		Name: "remote-port",
		Desc: "Ports for remote service. Support for configuring multiple ports, separated by commas or connector representing ranges, for example: 80,8000-8080",
	},
	&exec.ExpFlag{
		Name: "exclude-port",
		Desc: "Exclude local ports. Support for configuring multiple ports, separated by commas or connector representing ranges, for example: 22,8000. This flag is invalid when --local-port or --remote-port is specified",
	},
	&exec.ExpFlag{
		Name: "destination-ip",
		Desc: "destination ip. Support for using mask to specify the ip range, for example, 192.168.1.0/24. You can also use 192.168.1.1 or 192.168.1.1/32 to specify it.",
	},
	&exec.ExpFlag{
		Name:     "interface",
		Desc:     "Network interface, for example, eth0",
		Required: true,
	},
}

func getCommArgs(localPort, remotePort, excludePort, destinationIp string, args string) (string, error) {
	if localPort != "" {
		localPorts, err := util.ParseIntegerListToStringSlice(localPort)
		if err != nil {
			return "", err
		}
		args = fmt.Sprintf("%s --local-port %s", args, strings.Join(localPorts, ","))
	}
	if remotePort != "" {
		remotePorts, err := util.ParseIntegerListToStringSlice(remotePort)
		if err != nil {
			return "", err
		}
		args = fmt.Sprintf("%s --remote-port %s", args, strings.Join(remotePorts, ","))
	}
	if excludePort != "" {
		excludePorts, err := util.ParseIntegerListToStringSlice(excludePort)
		if err != nil {
			return "", err
		}
		args = fmt.Sprintf("%s --exclude-port %s", args, strings.Join(excludePorts, ","))
	}
	if destinationIp != "" {
		args = fmt.Sprintf("%s --destination-ip %s", args, destinationIp)
	}
	return args, nil
}
