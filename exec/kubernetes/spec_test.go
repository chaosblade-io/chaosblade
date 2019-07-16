package kubernetes

import (
	"testing"
	"os"

	"github.com/chaosblade-io/chaosblade/exec"
)

func TestCommandModelSpec_Name(t *testing.T) {
	spec := &CommandModelSpec{}
	models := &exec.Models{
		Version: "v1",
		Kind:    "plugin",
		Models:  make([]exec.ExpCommandModel, 0),
	}
	pflag := exec.ExpFlag{
		Name: "kubeconfig",
		Desc: "kube config",
	}
	prepare := exec.ExpPrepareModel{
		PrepareType:  "k8s",
		PrepareFlags: []exec.ExpFlag{pflag},
	}
	model := exec.ExpCommandModel{
		ExpName:         spec.Name(),
		ExpShortDesc:    spec.ShortDesc(),
		ExpLongDesc:     spec.LongDesc(),
		ExpExample:      spec.Example(),
		ExpActions:      make([]exec.ActionModel, 0),
		ExpScope:        "host",
		ExpPrepareModel: prepare,
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
		}
		model.ExpActions = append(model.ExpActions, actionModel)
	}
	models.Models = append(models.Models, model, model)
	exec.MarshalModelSpec(models, os.Stdout)
}
