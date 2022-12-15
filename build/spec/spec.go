/*
 * Copyright 1999-2020 Alibaba Group Holding Ltd.
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 */

package main

import (
	"fmt"
	"log"
	"os"
	"path"

	"github.com/chaosblade-io/chaosblade-exec-cri/exec"
	"github.com/chaosblade-io/chaosblade-spec-go/spec"
	"github.com/chaosblade-io/chaosblade-spec-go/util"

	"github.com/chaosblade-io/chaosblade/cli/cmd"
)

var version = "1.7.1"

func main() {

	if len(os.Args) < 3 {
		log.Panicln("less yaml file path, first parameter is scenario files and the second is the target yaml file")
	}
	filePath := os.Args[1]
	targetPath := os.Args[2]
	jvmSpecFile := path.Join(filePath, fmt.Sprintf("chaosblade-jvm-spec-%s.yaml", version))
	osSpecFile := path.Join(filePath, fmt.Sprintf("chaosblade-os-spec-%s.yaml", version))
	cloudSpecFile := path.Join(filePath, fmt.Sprintf("chaosblade-cloud-spec-%s.yaml", version))
	k8sSpecFile := path.Join(filePath, fmt.Sprintf("chaosblade-k8s-spec-%s.yaml", version))
	criSpecFile := path.Join(filePath, fmt.Sprintf("chaosblade-cri-spec-%s.yaml", version))
	cplusSpecFile := path.Join(filePath, "chaosblade-cplus-spec.yaml")
	chaosSpecFile := path.Join(targetPath, "chaosblade.spec.yaml")

	osModels := getOsModels(osSpecFile)
	cloudModels := getCloudModels(cloudSpecFile)
	jvmModels := getJvmModels(jvmSpecFile)
	cplusModels := getCplusModels(cplusSpecFile)
	criModels := getCriModels(criSpecFile, jvmSpecFile)
	k8sModels := getKubernetesModels(k8sSpecFile, jvmSpecFile)

	models := mergeModels(osModels, cloudModels, jvmModels, cplusModels, criModels, k8sModels)

	file, err := os.OpenFile(chaosSpecFile, os.O_CREATE|os.O_TRUNC|os.O_RDWR, 0755)
	if err != nil {
		log.Fatalf("open %s file err, %s", chaosSpecFile, err.Error())
	}
	defer file.Close()
	util.MarshalModelSpec(models, file)
}

func getOsModels(osSpecFile string) *spec.Models {
	models, err := util.ParseSpecsToModel(osSpecFile, nil)
	if err != nil {
		log.Fatalf("parse os spec failed, %s", err)
	}
	return models
}

func getCloudModels(cloudSpecFile string) *spec.Models {
	models, err := util.ParseSpecsToModel(cloudSpecFile, nil)
	if err != nil {
		log.Fatalf("parse cloud spec failed, %s", err)
	}
	return models
}

func getJvmModels(jvmSpecFile string) *spec.Models {
	models, err := util.ParseSpecsToModel(jvmSpecFile, nil)
	if err != nil {
		log.Fatalf("parse java spec failed, %s", err)
	}
	return models
}

func getCplusModels(cplusSpecFile string) *spec.Models {
	models, err := util.ParseSpecsToModel(cplusSpecFile, nil)
	if err != nil {
		log.Fatalf("parse cplus spec failed, %s", err)
	}
	return models
}

func getCriModels(criSpecFile, jvmSpecFile string) *spec.Models {
	criModels, err := util.ParseSpecsToModel(criSpecFile, nil)
	if err != nil {
		log.Fatalf("parse cri spec failed, %s", err)
	}

	jvmModels := getJvmModels(jvmSpecFile)
	for idx := range jvmModels.Models {
		model := &jvmModels.Models[idx]
		model.ExpScope = "cri"
		spec.AddFlagsToModelSpec(exec.GetExecInContainerFlags, model)
		addFlagToActionSpec(model)
		criModels.Models = append(criModels.Models, *model)
	}

	return criModels
}

func getKubernetesModels(k8sSpecFile, jvmSpecFile string) *spec.Models {
	models, err := util.ParseSpecsToModel(k8sSpecFile, nil)
	if err != nil {
		log.Fatalf("parse kubernetes spec failed, %s", err)
	}

	jvmModels := getJvmModels(jvmSpecFile)
	for idx := range jvmModels.Models {
		model := &jvmModels.Models[idx]

		model.ExpScope = "container"
		spec.AddFlagsToModelSpec(cmd.GetResourceFlags, model)
		addFlagToActionSpec(model)
		models.Models = append(models.Models, *model)
	}
	return models
}

func addFlagToActionSpec(model *spec.ExpCommandModel) {
	for idx := range model.ExpActions {
		action := &model.ExpActions[idx]
		flags := model.ExpFlags
		if flags == nil {
			flags = make([]spec.ExpFlag, 0)
		}
		action.ActionFlags = append(action.ActionFlags, flags...)
		//model.ExpActions[idx] = *action
	}
	model.SetFlags(nil)
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
			ActionExample:   action.Example(),
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
