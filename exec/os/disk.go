package os

import (
	"github.com/chaosblade-io/chaosblade/exec"
)

type DiskCommandSpec struct {
}

func (*DiskCommandSpec) Name() string {
	return "disk"
}

func (*DiskCommandSpec) ShortDesc() string {
	return "Disk experiment"
}

func (*DiskCommandSpec) LongDesc() string {
	return "Disk experiment contains fill disk or burn io"
}

func (*DiskCommandSpec) Example() string {
	return `disk fill --path /home --size 1000

# You can execute "blade query disk mount-point" command to query the mount points`
}

func (*DiskCommandSpec) Actions() []exec.ExpActionCommandSpec {
	return []exec.ExpActionCommandSpec{
		&FillActionSpec{},
		&BurnActionSpec{},
	}
}

func (*DiskCommandSpec) Flags() []exec.ExpFlagSpec {
	return []exec.ExpFlagSpec{}
}

func (*DiskCommandSpec) PreExecutor() exec.PreExecutor {
	return nil
}
