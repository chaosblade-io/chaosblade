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
	"github.com/chaosblade-io/chaosblade-spec-go/spec"
	"github.com/spf13/cobra"
)

type ServerCommand struct {
	baseCommand
}

func (sc *ServerCommand) Init() {
	sc.command = &cobra.Command{
		Use:     "server",
		Short:   "Server mode starts, exposes web services",
		Long:    "Server mode starts, exposes web services. Under the mode, you can send http request to trigger experiments",
		Aliases: []string{"srv"},
		RunE: func(cmd *cobra.Command, args []string) error {
			return spec.ResponseFailWithFlags(spec.CommandIllegal, "less start or stop command")
		},
		Example: serverExample(),
	}
}

func serverExample() string {
	return `blade server start --port 8000`
}
