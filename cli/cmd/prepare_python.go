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

package cmd

import (
	"context"
	"path/filepath"
	"strconv"

	"github.com/spf13/cobra"

	"github.com/chaosblade-io/chaosblade-spec-go/log"
	"github.com/chaosblade-io/chaosblade-spec-go/spec"
	"github.com/chaosblade-io/chaosblade-spec-go/util"

	"github.com/chaosblade-io/chaosblade/exec/python"
)

type PreparePythonCommand struct {
	baseCommand
	port         int
	pythonPath   string
	targetScript string
}

func (pc *PreparePythonCommand) Init() {
	pc.command = &cobra.Command{
		Use:   "python",
		Short: "Activate python agent.",
		Long:  "Activate python agent.",
		RunE: func(cmd *cobra.Command, args []string) error {
			return pc.preparePython()
		},
		Example: pc.prepareExample(),
	}
	pc.command.Flags().IntVarP(&pc.port, "port", "p", 9526, "the server port of python agent")
	pc.command.Flags().StringVar(&pc.pythonPath, "python-path", "", "the path of python interpreter")
	pc.command.Flags().StringVar(&pc.targetScript, "target-script", "", "the path of target python script")
	pc.command.MarkFlagRequired("python-path")
	pc.command.MarkFlagRequired("target-script")
}

func (pc *PreparePythonCommand) prepareExample() string {
	return `prepare python --port 9526 --python-path /path/to/python --target-script /path/to/script.py`
}

func (pc *PreparePythonCommand) preparePython() error {
	ctx := context.Background()
	portStr := strconv.Itoa(pc.port)

	// Convert target-script to absolute path to ensure revoke works correctly
	// regardless of the current working directory
	absTargetScript, err := filepath.Abs(pc.targetScript)
	if err != nil {
		log.Errorf(ctx, "failed to resolve absolute path for %s: %v", pc.targetScript, err)
		return spec.ResponseFailWithFlags(spec.ParameterInvalid, "target-script", pc.targetScript, "cannot resolve absolute path")
	}

	record, err := GetDS().QueryRunningPreByTypeAndProcess(python.PreparePythonType, portStr, "")
	if err != nil {
		log.Errorf(ctx, "%s", spec.DatabaseError.Sprintf("query", err))
		return spec.ResponseFailWithFlags(spec.DatabaseError, "query", err)
	}
	if record == nil || record.Status != Running {
		record, err = insertPrepareRecord(python.PreparePythonType, portStr, portStr, absTargetScript)
		if err != nil {
			log.Errorf(ctx, util.GetRunFuncName(), spec.DatabaseError.Sprintf("insert", err))
			return spec.ResponseFailWithFlags(spec.DatabaseError, "insert", err)
		}
	}
	ctx = context.WithValue(ctx, spec.Uid, record.Uid)
	response := python.Prepare(ctx, portStr, pc.pythonPath, absTargetScript)
	return handlePrepareResponse(ctx, pc.command, response)
}
