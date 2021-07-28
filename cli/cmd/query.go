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

// QueryCommand defines query command
type QueryCommand struct {
	baseCommand
}

// Init attach command operators includes create instance and bind flags
func (qc *QueryCommand) Init() {
	qc.command = &cobra.Command{
		Use:     "query TARGET TYPE",
		Aliases: []string{"q"},
		Short:   "Query the parameter values required for chaos experiments",
		Long:    "Query the parameter values required for chaos experiments",
		RunE: func(cmd *cobra.Command, args []string) error {
			return spec.ResponseFailWithFlags(spec.CommandIllegal, "less target type")
		},
		Example: qc.queryExample(),
	}
}

func (qc *QueryCommand) queryExample() string {
	return `query network interface`
}
