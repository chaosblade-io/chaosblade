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
	return "disk fill --mount-on / --size 1000"
}

func (*DiskCommandSpec) Actions() []exec.ExpActionCommandSpec {
	return []exec.ExpActionCommandSpec{
		&FillActionSpec{},
		&BurnActionSpec{},
	}
}

func (*DiskCommandSpec) Flags() []exec.ExpFlagSpec {
	return []exec.ExpFlagSpec{
		&exec.ExpFlag{
			Name: "mount-on",
			Desc: "the disk device mounted",
		},
	}
}

func (*DiskCommandSpec) PreExecutor() exec.PreExecutor {
	return nil
}
