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
	"context"
	"fmt"
	"strings"

	"github.com/chaosblade-io/chaosblade-spec-go/channel"
	"github.com/chaosblade-io/chaosblade-spec-go/spec"
	"github.com/spf13/cobra"

	"github.com/chaosblade-io/chaosblade/exec/cplus"
	"github.com/chaosblade-io/chaosblade/exec/jvm"
)

type RevokeCommand struct {
	baseCommand
}

func (rc *RevokeCommand) Init() {
	rc.command = &cobra.Command{
		Use:     "revoke [PREPARE UID]",
		Aliases: []string{"r"},
		Short:   "Undo chaos engineering experiment preparation",
		Long:    "Undo chaos engineering experiment preparation",
		Args:    cobra.MinimumNArgs(1),
		RunE: func(cmd *cobra.Command, args []string) error {
			return rc.runRevoke(args)
		},
		Example: revokeExample(),
	}
}

func (rc *RevokeCommand) runRevoke(args []string) error {
	uid := args[0]
	ctx := context.WithValue(context.Background(), spec.Uid, uid)
	record, err := GetDS().QueryPreparationByUid(uid)
	if err != nil {
		return spec.ResponseFailWithFlags(spec.DatabaseError, "query", err)
	}
	if record == nil {
		return spec.ResponseFailWithFlags(spec.DataNotFound, uid)
	}
	if record.Status == Revoked {
		rc.command.Println(spec.ReturnSuccess("success").Print())
		return nil
	}
	var response *spec.Response
	var channel = channel.NewLocalChannel()
	switch record.ProgramType {
	case PrepareJvmType:
		response = jvm.Detach(ctx, record.Port)
	case PrepareCPlusType:
		response = cplus.Revoke(ctx, record.Port)
	case PrepareK8sType:
		args := fmt.Sprintf("delete ns chaosblade")
		response = channel.Run(ctx, "kubectl", args)
	default:
		return spec.ResponseFailWithFlags(spec.ParameterIllegal, "type", record.ProgramType, "not support the type")
	}
	if response.Success {
		checkError(GetDS().UpdatePreparationRecordByUid(uid, Revoked, ""))
	} else if strings.Contains(response.Err, "connection refused") {
		// sandbox has been detached, reset response value
		response = spec.ReturnSuccess("success")
		checkError(GetDS().UpdatePreparationRecordByUid(uid, Revoked, ""))
	} else {
		// other failed reason
		checkError(GetDS().UpdatePreparationRecordByUid(uid, record.Status, fmt.Sprintf("revoke failed. %s", response.Err)))
		return response
	}
	rc.command.Println(response.Print())
	return nil
}

func revokeExample() string {
	return `blade revoke cc015e9bd9c68406`
}
