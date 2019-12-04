package main

import (
	"fmt"
	os2 "os"
	"path"

	"github.com/chaosblade-io/chaosblade-spec-go/spec"
	"github.com/chaosblade-io/chaosblade-spec-go/util"
	"github.com/sirupsen/logrus"
)

var version = "0.4.0"
var filePath = fmt.Sprintf("%s/chaosblade-%s/bin", "/Users/Shared/ChaosBladeProjects/chaosblade-opensource/chaosblade/src/github.com/chaosblade-io/chaosblade/target", version)
var targetPath = "/Users/Shared/ChaosBladeProjects/chaosblade-opensource/chaosblade/src/github.com/chaosblade-io/chaosblade/build/spec"
var jvmSpecFile = path.Join(filePath, "jvm.spec.yaml")
var osSpecFile = path.Join(filePath, fmt.Sprintf("chaosblade-os-spec-%s.yaml", version))
var k8sSpecFile = path.Join(filePath, fmt.Sprintf("chaosblade-k8s-spec-%s.yaml", version))
var dockerSpecFile = path.Join(filePath, fmt.Sprintf("chaosblade-docker-spec-%s.yaml", version))
var nodeSpecFile = path.Join(filePath, "nodejs-chaosblade.spec.yaml")
var cplusSpecFile = path.Join(filePath, "chaosblade-cplus-spec.yaml ")
var chaosSpecFile = path.Join(targetPath, "chaosblade.spec.yaml")

func main() {
	osModels := getOsModels()
	jvmModels := getJvmModels()
	//nodeModels := getNodeModels()
	cplusModels := getCplusModels()
	dockerModels := getDockerModels()
	k8sModels := getKubernetesModels()

	models := mergeModels(osModels, jvmModels, dockerModels, k8sModels, cplusModels)

	file, err := os2.OpenFile(chaosSpecFile, os2.O_CREATE|os2.O_TRUNC|os2.O_RDWR, 0755)
	if err != nil {
		logrus.Fatalf("open %s file err, %s", chaosSpecFile, err.Error())
	}
	defer file.Close()
	util.MarshalModelSpec(models, file)
}

func getOsModels() *spec.Models {
	models, err := util.ParseSpecsToModel(osSpecFile, nil)
	if err != nil {
		logrus.Fatalf("parse os spec failed, %s", err)
	}
	return models
}
func getJvmModels() *spec.Models {
	models, err := util.ParseSpecsToModel(jvmSpecFile, nil)
	if err != nil {
		logrus.Fatalf("parse java spec failed, %s", err)
	}
	return models
}

func getNodeModels() *spec.Models {
	models, err := util.ParseSpecsToModel(nodeSpecFile, nil)
	if err != nil {
		logrus.Fatalf("parse node spec failed, %s", err)
	}
	return models
}

func getCplusModels() *spec.Models {
	models, err := util.ParseSpecsToModel(cplusSpecFile, nil)
	if err != nil {
		logrus.Fatalf("parse node spec failed, %s", err)
	}
	return models
}

func getDockerModels() *spec.Models {
	models, err := util.ParseSpecsToModel(dockerSpecFile, nil)
	if err != nil {
		logrus.Fatalf("parse docker spec failed, %s", err)
	}
	return models
}

func getKubernetesModels() *spec.Models {
	models, err := util.ParseSpecsToModel(k8sSpecFile, nil)
	if err != nil {
		logrus.Fatalf("parse kubernetes spec failed, %s", err)
	}
	return models
}

func convertSpecToModels(modelSpec spec.ExpModelCommandSpec, prepare spec.ExpPrepareModel) *spec.Models {
	models := &spec.Models{
		Version: "v1",
		Kind:    "plugin",
		Models:  make([]spec.ExpCommandModel, 0),
	}
	model := spec.ExpCommandModel{
		ExpName:         modelSpec.Name(),
		ExpShortDesc:    modelSpec.ShortDesc(),
		ExpLongDesc:     modelSpec.LongDesc(),
		ExpExample:      modelSpec.Example(),
		ExpActions:      make([]spec.ActionModel, 0),
		ExpSubTargets:   make([]string, 0),
		ExpPrepareModel: prepare,
		ExpScope:        modelSpec.Scope(),
	}
	for _, action := range modelSpec.Actions() {
		actionModel := spec.ActionModel{
			ActionName:      action.Name(),
			ActionAliases:   action.Aliases(),
			ActionShortDesc: action.ShortDesc(),
			ActionLongDesc:  action.LongDesc(),
			ActionMatchers: func() []spec.ExpFlag {
				matchers := make([]spec.ExpFlag, 0)
				for _, m := range action.Matchers() {
					matchers = append(matchers, spec.ExpFlag{
						Name:     m.FlagName(),
						Desc:     m.FlagDesc(),
						NoArgs:   m.FlagNoArgs(),
						Required: m.FlagRequired(),
					})
				}
				return matchers
			}(),
			ActionFlags: func() []spec.ExpFlag {
				flags := make([]spec.ExpFlag, 0)
				for _, m := range action.Flags() {
					flags = append(flags, spec.ExpFlag{
						Name:     m.FlagName(),
						Desc:     m.FlagDesc(),
						NoArgs:   m.FlagNoArgs(),
						Required: m.FlagRequired(),
					})
				}
				for _, m := range modelSpec.Flags() {
					flags = append(flags, spec.ExpFlag{
						Name:     m.FlagName(),
						Desc:     m.FlagDesc(),
						NoArgs:   m.FlagNoArgs(),
						Required: m.FlagRequired(),
					})
				}
				flags = append(flags,
					spec.ExpFlag{
						Name:     "timeout",
						Desc:     "set timeout for experiment",
						Required: false,
					},
				)
				return flags
			}(),
		}
		model.ExpActions = append(model.ExpActions, actionModel)
	}
	models.Models = append(models.Models, model)
	return models
}

func addModels(parent *spec.Models, child *spec.Models) {
	for idx, model := range parent.Models {
		for _, sub := range child.Models {
			model.ExpSubTargets = append(model.ExpSubTargets, sub.ExpName)
		}
		parent.Models[idx] = model
	}
}

func mergeModels(models ...*spec.Models) *spec.Models {
	result := &spec.Models{
		Models: make([]spec.ExpCommandModel, 0),
	}
	for _, model := range models {
		result.Version = model.Version
		result.Kind = model.Kind
		result.Models = append(result.Models, model.Models...)
	}
	return result
}
