/*
 * Copyright 2025 The ChaosBlade Authors
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

	"github.com/chaosblade-io/chaosblade-spec-go/spec"
	"github.com/chaosblade-io/chaosblade-spec-go/util"

	"github.com/chaosblade-io/chaosblade/version"
)

func main() {
	if len(os.Args) < 3 {
		log.Panicln("less yaml file path, first parameter is scenario files and the second is the target yaml file")
	}
	filePath := os.Args[1]
	targetPath := os.Args[2]
	jvmSpecFile := path.Join(filePath, fmt.Sprintf("chaosblade-jvm-spec-%s.yaml", version.Ver))
	osSpecFile := path.Join(filePath, fmt.Sprintf("chaosblade-os-spec-%s.yaml", version.Ver))
	cloudSpecFile := path.Join(filePath, fmt.Sprintf("chaosblade-cloud-spec-%s.yaml", version.Ver))
	middlewareSpecFile := path.Join(filePath, fmt.Sprintf("chaosblade-middleware-spec-%s.yaml", version.Ver))
	k8sSpecFile := path.Join(filePath, fmt.Sprintf("chaosblade-k8s-spec-%s.yaml", version.Ver))
	criSpecFile := path.Join(filePath, fmt.Sprintf("chaosblade-cri-spec-%s.yaml", version.Ver))
	cplusSpecFile := path.Join(filePath, fmt.Sprintf("chaosblade-cplus-spec-%s.yaml", version.Ver))
	chaosSpecFile := path.Join(targetPath, "chaosblade.spec.yaml")

	osModels := getOsModels(osSpecFile)
	cloudModels := getCloudModels(cloudSpecFile)
	middlewareModels := getMiddlewareModels(middlewareSpecFile)
	jvmModels := getJvmModels(jvmSpecFile)
	cplusModels := getCplusModels(cplusSpecFile)
	criModels := getCriModels(criSpecFile, jvmSpecFile)
	k8sModels := getKubernetesModels(k8sSpecFile, jvmSpecFile)

	models := mergeModels(osModels, cloudModels, middlewareModels, jvmModels, cplusModels, criModels, k8sModels)

	file, err := os.OpenFile(chaosSpecFile, os.O_CREATE|os.O_TRUNC|os.O_RDWR, 0o755)
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

func getMiddlewareModels(middlewareSpecFile string) *spec.Models {
	models, err := util.ParseSpecsToModel(middlewareSpecFile, nil)
	if err != nil {
		log.Fatalf("parse middleware spec failed, %s", err)
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
		// CPlus spec file may not exist, return empty models
		if os.IsNotExist(err) {
			log.Printf("cplus spec file not found, skipping: %s", cplusSpecFile)
			return &spec.Models{
				Version: "v1",
				Kind:    "plugin",
				Models:  make([]spec.ExpCommandModel, 0),
			}
		}
		log.Fatalf("parse cplus spec failed, %s", err)
	}
	return models
}

func getCriModels(criSpecFile, jvmSpecFile string) *spec.Models {
	criModels, err := util.ParseSpecsToModel(criSpecFile, nil)
	if err != nil {
		log.Fatalf("parse cri spec failed, %s", err)
	}

	// Extract container flags from CRI models
	containerFlags := extractContainerFlags(criModels)

	jvmModels := getJvmModels(jvmSpecFile)
	for idx := range jvmModels.Models {
		model := &jvmModels.Models[idx]
		model.ExpScope = "cri"
		// Add container flags to model
		model.ExpFlags = append(model.ExpFlags, containerFlags...)
		addFlagToActionSpec(model)
		criModels.Models = append(criModels.Models, *model)
	}

	return criModels
}

// extractContainerFlags extracts container-related flags from CRI models
func extractContainerFlags(criModels *spec.Models) []spec.ExpFlag {
	containerFlagNames := map[string]bool{
		"container-id":             true,
		"container-name":           true,
		"cri-endpoint":             true,
		"container-runtime":        true,
		"container-namespace":      true,
		"container-label-selector": true,
	}

	flagsMap := make(map[string]spec.ExpFlag)
	for _, model := range criModels.Models {
		for _, action := range model.ExpActions {
			for _, flag := range action.ActionFlags {
				if containerFlagNames[flag.Name] {
					flagsMap[flag.Name] = flag
				}
			}
		}
	}

	// Convert map to slice, maintaining a consistent order
	orderedFlags := []spec.ExpFlag{
		{Name: "container-id", Desc: "Container id, when used with container-name, container-id is preferred", Required: false},
		{Name: "container-name", Desc: "Container name, when used with container-id, container-id is preferred", Required: false},
		{Name: "cri-endpoint", Desc: "Cri container socket endpoint", Required: false},
		{Name: "container-runtime", Desc: "container runtime, support cri and containerd, default value is docker", Required: false},
		{Name: "container-namespace", Desc: "container namespace, If container-runtime is containerd it will be used, default value is k8s.io", Required: false},
		{Name: "container-label-selector", Desc: "Container label selector, when used with container-id or container-name, container-id or container-name is preferred", Required: false},
	}

	// Use flags from yaml if available, otherwise use defaults
	result := make([]spec.ExpFlag, 0, len(orderedFlags))
	for _, flag := range orderedFlags {
		if yamlFlag, ok := flagsMap[flag.Name]; ok {
			result = append(result, yamlFlag)
		} else {
			result = append(result, flag)
		}
	}

	return result
}

func getKubernetesModels(k8sSpecFile, jvmSpecFile string) *spec.Models {
	models, err := util.ParseSpecsToModel(k8sSpecFile, nil)
	if err != nil {
		log.Fatalf("parse kubernetes spec failed, %s", err)
	}

	// Extract resource flags from K8s models
	resourceFlags := extractResourceFlags(models)

	jvmModels := getJvmModels(jvmSpecFile)
	for idx := range jvmModels.Models {
		model := &jvmModels.Models[idx]

		model.ExpScope = "container"
		// Add resource flags to model
		model.ExpFlags = append(model.ExpFlags, resourceFlags...)
		addFlagToActionSpec(model)
		models.Models = append(models.Models, *model)
	}
	return models
}

// extractResourceFlags extracts Kubernetes resource-related flags from K8s models
func extractResourceFlags(k8sModels *spec.Models) []spec.ExpFlag {
	resourceFlagNames := map[string]bool{
		"names":           true,
		"labels":          true,
		"namespace":       true,
		"kind":            true,
		"container-names": true,
		"container-ids":   true,
		"evict-count":     true,
		"evict-percent":   true,
	}

	flagsMap := make(map[string]spec.ExpFlag)
	for _, model := range k8sModels.Models {
		for _, action := range model.ExpActions {
			for _, flag := range action.ActionFlags {
				if resourceFlagNames[flag.Name] {
					flagsMap[flag.Name] = flag
				}
			}
			for _, flag := range action.ActionMatchers {
				if resourceFlagNames[flag.Name] {
					flagsMap[flag.Name] = flag
				}
			}
		}
	}

	// Convert map to slice, maintaining a consistent order
	orderedFlags := []spec.ExpFlag{
		{Name: "names", Desc: "Resource names, such as pod name. You must add namespace flag for it. Multiple parameters are separated directly by commas", Required: false},
		{Name: "labels", Desc: "Label selector, the relationship between values that are or", Required: false},
		{Name: "namespace", Desc: "Namespace, such as default, for namespaced resources", Required: false},
		{Name: "kind", Desc: "Resource kind, such as deployment", Required: false},
		{Name: "container-names", Desc: "Container names, such as nginx-container", Required: false},
		{Name: "container-ids", Desc: "Container ids", Required: false},
		{Name: "evict-count", Desc: "Count of affected resource", Required: false},
		{Name: "evict-percent", Desc: "Percent of affected resource, integer value without %", Required: false},
	}

	// Use flags from yaml if available, otherwise use defaults
	result := make([]spec.ExpFlag, 0, len(orderedFlags))
	for _, flag := range orderedFlags {
		if yamlFlag, ok := flagsMap[flag.Name]; ok {
			result = append(result, yamlFlag)
		} else {
			result = append(result, flag)
		}
	}

	return result
}

func addFlagToActionSpec(model *spec.ExpCommandModel) {
	for idx := range model.ExpActions {
		action := &model.ExpActions[idx]
		flags := model.ExpFlags
		if flags == nil {
			flags = make([]spec.ExpFlag, 0)
		}
		action.ActionFlags = append(action.ActionFlags, flags...)
		// model.ExpActions[idx] = *action
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
