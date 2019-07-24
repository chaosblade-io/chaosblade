package main

import (
	"context"
	"flag"
	"fmt"

	"github.com/chaosblade-io/chaosblade/exec"
	"github.com/chaosblade-io/chaosblade/exec/os/bin"
)

var dnsDomain, dnsIp string
var changeDnsStart, changeDnsStop bool

func main() {
	flag.StringVar(&dnsDomain, "domain", "", "dns domain")
	flag.StringVar(&dnsIp, "ip", "", "dns ip")
	flag.BoolVar(&changeDnsStart, "start", false, "start change dns")
	flag.BoolVar(&changeDnsStop, "stop", false, "recover dns")
	flag.Parse()

	if dnsDomain == "" || dnsIp == "" {
		bin.PrintErrAndExit("less --domain or --ip flag")
	}
	if changeDnsStart {
		startChangeDns(dnsDomain, dnsIp)
	} else if changeDnsStop {
		recoverDns(dnsDomain, dnsIp)
	} else {
		bin.PrintErrAndExit("less --start or --stop flag")
	}
}

const hosts = "/etc/hosts"
const tmpHosts = "/tmp/chaos-hosts.tmp"

var channel = exec.NewLocalChannel()

// startChangeDns by the domain and ip
func startChangeDns(domain, ip string) {
	ctx := context.Background()
	dnsPair := createDnsPair(domain, ip)
	response := channel.Run(ctx, "grep", fmt.Sprintf(`-q "%s" %s`, dnsPair, hosts))
	if response.Success {
		bin.PrintErrAndExit(fmt.Sprintf("%s has been exist", dnsPair))
		return
	}
	response = channel.Run(ctx, "echo", fmt.Sprintf(`"%s" >> %s`, dnsPair, hosts))
	if !response.Success {
		bin.PrintErrAndExit(response.Err)
		return
	}
	bin.PrintOutputAndExit(response.Result.(string))
}

// recoverDns
func recoverDns(domain, ip string) {
	ctx := context.Background()
	dnsPair := createDnsPair(domain, ip)
	response := channel.Run(ctx, "grep", fmt.Sprintf(`-q "%s" %s`, dnsPair, hosts))
	if !response.Success {
		bin.PrintOutputAndExit("nothing to do")
		return
	}
	response = channel.Run(ctx, "cat", fmt.Sprintf(`%s | grep -v "%s" > %s && cat %s > %s`,
		hosts, dnsPair, tmpHosts, tmpHosts, hosts))
	if !response.Success {
		bin.PrintErrAndExit(response.Err)
		return
	}
	channel.Run(ctx, "rm", fmt.Sprintf(`-rf %s`, tmpHosts))
}

func createDnsPair(domain, ip string) string {
	return fmt.Sprintf("%s %s #chaosblade", ip, domain)
}
