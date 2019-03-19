package docker

import (
	"testing"

	"github.com/chaosblade-io/chaosblade/exec"
	"os"
)

func TestCommandModelSpec_Name(t *testing.T) {
	spec := &CommandModelSpec{}
	models := &exec.Models{
		Version: "v1",
		Kind:    "plugin",
		Models:  make([]exec.ExpCommandModel, 0),
	}
	model := exec.ExpCommandModel{
		ExpName:      spec.Name(),
		ExpShortDesc: spec.ShortDesc(),
		ExpLongDesc:  spec.LongDesc(),
		ExpExample:   spec.Example(),
		ExpActions:   make([]exec.ActionModel, 0),
	}
	for _, action := range spec.Actions() {
		actionModel := exec.ActionModel{
			ActionName:      action.Name(),
			ActionAliases:   action.Aliases(),
			ActionShortDesc: action.ShortDesc(),
			ActionLongDesc:  action.LongDesc(),
			ActionMatchers: func() []exec.ExpFlag {
				matchers := make([]exec.ExpFlag, 0)
				for _, m := range action.Matchers() {
					matchers = append(matchers, exec.ExpFlag{
						Name:     m.FlagName(),
						Desc:     m.FlagDesc(),
						NoArgs:   m.FlagNoArgs(),
						Required: m.FlagRequired(),
					})
				}
				return matchers
			}(),
			ActionFlags: func() []exec.ExpFlag {
				flags := make([]exec.ExpFlag, 0)
				for _, m := range action.Flags() {
					flags = append(flags, exec.ExpFlag{
						Name:     m.FlagName(),
						Desc:     m.FlagDesc(),
						NoArgs:   m.FlagNoArgs(),
						Required: m.FlagRequired(),
					})
				}
				return flags
			}(),
		}
		model.ExpActions = append(model.ExpActions, actionModel)
	}
	models.Models = append(models.Models, model, model)
	exec.MarshalModelSpec(models, os.Stdout)
}
