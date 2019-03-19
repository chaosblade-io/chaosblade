package main

import (
	"fmt"
	"flag"
	"github.com/chaosblade-io/chaosblade/exec"
	"context"
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
		printErrAndExit("less --domain or --ip flag")
	}
	if changeDnsStart {
		startChangeDns(dnsDomain, dnsIp)
	} else if changeDnsStop {
		recoverDns(dnsDomain, dnsIp)
	} else {
		printErrAndExit("less --start or --stop flag")
	}
}

const hosts = "/etc/hosts"
const tmpHosts = "/tmp/chaos-hosts.tmp"

// startChangeDns by the domain and ip
func startChangeDns(domain, ip string) {
	channel := exec.NewLocalChannel()
	ctx := context.Background()
	dnsPair := createDnsPair(domain, ip)
	response := channel.Run(ctx, "grep", fmt.Sprintf(`-q "%s" %s`, dnsPair, hosts))
	if response.Success {
		printErrAndExit(fmt.Sprintf("%s has been exist", dnsPair))
	}
	response = channel.Run(ctx, "echo", fmt.Sprintf(`"%s" >> %s`, dnsPair, hosts))
	if !response.Success {
		printErrAndExit(response.Err)
	}
	printOutputAndExit(response.Result.(string))
}

// recoverDns
func recoverDns(domain, ip string) {
	channel := exec.NewLocalChannel()
	ctx := context.Background()
	dnsPair := createDnsPair(domain, ip)
	response := channel.Run(ctx, "grep", fmt.Sprintf(`-q "%s" %s`, dnsPair, hosts))
	if !response.Success {
		printOutputAndExit("nothing to do")
	}
	response = channel.Run(ctx, "cat", fmt.Sprintf(`%s | grep -v "%s" > %s && cat %s > %s`,
		hosts, dnsPair, tmpHosts, tmpHosts, hosts))
	if !response.Success {
		printErrAndExit(response.Err)
	}
	channel.Run(ctx, "rm", fmt.Sprintf(`-rf %s`, tmpHosts))
}

func createDnsPair(domain, ip string) string {
	return fmt.Sprintf("%s %s #chaosblade", ip, domain)
}
