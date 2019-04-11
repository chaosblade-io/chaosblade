package os

import "github.com/chaosblade-io/chaosblade/exec"

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
	return "network delay --interface eth0 --time 3000"
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
