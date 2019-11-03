package docker

import "github.com/chaosblade-io/chaosblade-spec-go/spec"

type CommandModelSpec struct {
	spec.BaseExpModelCommandSpec
}

func NewCommandModelSpec() spec.ExpModelCommandSpec {
	return &CommandModelSpec{
		spec.BaseExpModelCommandSpec{
			ExpActions: []spec.ExpActionCommandSpec{},
			ExpFlags:   []spec.ExpFlagSpec{},
		},
	}
}

func (*CommandModelSpec) Name() string {
	return "docker"
}

func (*CommandModelSpec) ShortDesc() string {
	return "Docker experiment"
}

func (*CommandModelSpec) LongDesc() string {
	return "Docker experiment, for example remove container"
}

func (*CommandModelSpec) Example() string {
	return "blade create docker remove --container-id 65eead213dd3"
}
