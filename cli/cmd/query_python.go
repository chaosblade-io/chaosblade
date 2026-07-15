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
	"errors"

	"github.com/spf13/cobra"

	"github.com/chaosblade-io/chaosblade-spec-go/spec"

	"github.com/chaosblade-io/chaosblade/exec/python"
)

type QueryPythonCommand struct {
	baseCommand
}

func (qpc *QueryPythonCommand) Init() {
	qpc.command = &cobra.Command{
		Use:   "python <UID>",
		Short: "Query status of the specify python preparation",
		Long:  "Query status of the specify python preparation",
		Args:  cobra.ExactArgs(1),
		RunE: func(cmd *cobra.Command, args []string) error {
			ctx := context.WithValue(context.Background(), spec.Uid, args[0])
			return qpc.queryPythonExpStatus(ctx, cmd)
		},
		Example: qpc.queryPythonExample(),
	}
}

func (qpc *QueryPythonCommand) queryPythonExample() string {
	return `blade query python 29c3f9dab4abbc79`
}

// queryPythonExpStatus by uid
func (qpc *QueryPythonCommand) queryPythonExpStatus(ctx context.Context, command *cobra.Command) error {
	uid := ctx.Value(spec.Uid).(string)
	record, err := GetDS().QueryPreparationByUid(uid)
	if err != nil {
		return spec.ResponseFailWithFlags(spec.DatabaseError, "query", err)
	}
	if record == nil {
		return spec.ResponseFailWithFlags(spec.DataNotFound, uid)
	}
	response := python.Status(ctx, record.Port)
	if response.Success {
		command.Println(response.Print())
	} else {
		return errors.New(response.Error())
	}
	return nil
}
