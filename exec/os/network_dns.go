package os

import (
	"github.com/chaosblade-io/chaosblade/exec"
	"github.com/chaosblade-io/chaosblade/transport"
	"context"
	"path"
	"fmt"
)

type DnsActionSpec struct {
}

func (*DnsActionSpec) Name() string {
	return "dns"
}

func (*DnsActionSpec) Aliases() []string {
	return []string{}
}

func (*DnsActionSpec) ShortDesc() string {
	return "Dns experiment"
}

func (*DnsActionSpec) LongDesc() string {
	return "Dns experiment"
}

func (*DnsActionSpec) Matchers() []exec.ExpFlagSpec {
	return []exec.ExpFlagSpec{}
}

func (*DnsActionSpec) Flags() []exec.ExpFlagSpec {
	return []exec.ExpFlagSpec{
		&exec.ExpFlag{
			Name:     "domain",
			Desc:     "Domain name",
			Required: true,
		},
		&exec.ExpFlag{
			Name:     "ip",
			Desc:     "Domain ip",
			Required: true,
		},
	}
}

type NetworkDnsExecutor struct {
	channel exec.Channel
}

func (*NetworkDnsExecutor) Name() string {
	return "dns"
}

var changeDnsBin = "chaos_changedns"

func (ns *NetworkDnsExecutor) Exec(uid string, ctx context.Context, model *exec.ExpModel) *transport.Response {
	if ns.channel == nil {
		return transport.ReturnFail(transport.Code[transport.ServerError], "channel is nil")
	}
	domain := model.ActionFlags["domain"]
	ip := model.ActionFlags["ip"]
	if domain == "" || ip == "" {
		return transport.ReturnFail(transport.Code[transport.IllegalParameters],
			"less domain or ip arg for dns injection")
	}
	if _, ok := exec.IsDestroy(ctx); ok {
		return ns.stop(ctx, domain, ip)
	}
	return ns.start(ctx, domain, ip)
}

func (ns *NetworkDnsExecutor) start(ctx context.Context, domain, ip string) *transport.Response {
	return ns.channel.Run(ctx, path.Join(ns.channel.GetScriptPath(), changeDnsBin),
		fmt.Sprintf("--start --domain %s --ip %s", domain, ip))
}

func (ns *NetworkDnsExecutor) stop(ctx context.Context, domain, ip string) *transport.Response {
	return ns.channel.Run(ctx, path.Join(ns.channel.GetScriptPath(), changeDnsBin),
		fmt.Sprintf("--stop --domain %s --ip %s", domain, ip))
}

func (ns *NetworkDnsExecutor) SetChannel(channel exec.Channel) {
	ns.channel = channel
}

func (*DnsActionSpec) Executor(channel exec.Channel) exec.Executor {
	return &NetworkDnsExecutor{channel}
}
