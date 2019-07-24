package os

import "github.com/chaosblade-io/chaosblade/exec"

type ProcessCommandModelSpec struct {
}

func (*ProcessCommandModelSpec) Name() string {
	return "process"
}

func (*ProcessCommandModelSpec) ShortDesc() string {
	return "Process experiment"
}

func (*ProcessCommandModelSpec) LongDesc() string {
	return "Process experiment, for example, kill process"
}

func (*ProcessCommandModelSpec) Example() string {
	return "process kill --process tomcat"
}

func (*ProcessCommandModelSpec) Actions() []exec.ExpActionCommandSpec {
	return []exec.ExpActionCommandSpec{
		&KillProcessActionCommandSpec{},
		&StopProcessActionCommandSpec{},
	}
}

func (*ProcessCommandModelSpec) Flags() []exec.ExpFlagSpec {
	return []exec.ExpFlagSpec{}
}

func (*ProcessCommandModelSpec) PreExecutor() exec.PreExecutor {
	return nil
}
