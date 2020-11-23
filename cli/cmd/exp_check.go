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
package cmd

import (
	"fmt"
	"path"

	"github.com/chaosblade-io/chaosblade-spec-go/spec"
	specutil "github.com/chaosblade-io/chaosblade-spec-go/util"

	"github.com/chaosblade-io/chaosblade/version"
)

var AllCheckModels *spec.Models

func newBaseExpCheckCommandService(actionService actionCommandService) *baseExpCommandService {
	service := &baseExpCommandService{
		commands:           make(map[string]*modelCommand, 0),
		executors:          make(map[string]spec.Executor, 0),
		bindFlagsFunc:      actionService.bindFlagsFunction(),
		actionRunEFunc:     actionService.actionRunEFunc,
		actionPostRunEFunc: actionService.actionPostRunEFunc,
	}
	service.registerSubCommandsForCheck()
	for _, command := range service.commands {
		actionService.CobraCmd().AddCommand(command.CobraCmd())
	}
	return service
}

func (ec *baseExpCommandService) registerSubCommandsForCheck() {
	ec.registerCheckExpCommands()
}

func (ec *baseExpCommandService) registerCheckExpCommands() []*modelCommand {
	var err error
	file := path.Join(specutil.GetYamlHome(), fmt.Sprintf("chaosblade-check-spec-%s.yaml", version.Ver))
	AllCheckModels, err = specutil.ParseSpecsToModel(file, nil)
	if err != nil {
		return nil
	}
	osCommands := make([]*modelCommand, 0)
	for idx := range AllCheckModels.Models {
		model := &AllCheckModels.Models[idx]
		command := ec.registerExpCommand(model, "")
		osCommands = append(osCommands, command)
	}
	return osCommands
}
