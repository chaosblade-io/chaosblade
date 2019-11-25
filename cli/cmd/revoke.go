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
	record, err := GetDS().QueryPreparationByUid(uid)
	if err != nil {
		return spec.ReturnFail(spec.Code[spec.DatabaseError],
			fmt.Sprintf("query record err, %s", err.Error()))
	}
	if record == nil {
		return spec.ReturnFail(spec.Code[spec.DataNotFound],
			fmt.Sprintf("the uid record not found"))
	}
	if record.Status == "Revoked" {
		rc.command.Println(spec.ReturnSuccess("success").Print())
		return nil
	}
	var response *spec.Response
	var channel = channel.NewLocalChannel()
	switch record.ProgramType {
	case PrepareJvmType:
		response = jvm.Detach(record.Port)
	case PrepareCPlusType:
		response = cplus.Revoke(record.Port)
	case PrepareK8sType:
		args := fmt.Sprintf("delete ns chaosblade")
		response = channel.Run(context.Background(), "kubectl", args)
	default:
		return spec.ReturnFail(spec.Code[spec.IllegalParameters],
			fmt.Sprintf("not support the %s type", record.ProgramType))
	}
	if response.Success {
		checkError(GetDS().UpdatePreparationRecordByUid(uid, "Revoked", ""))
	} else if strings.Contains(response.Err, "connection refused") {
		// sandbox has been detached, reset response value
		response = spec.ReturnSuccess("success")
		checkError(GetDS().UpdatePreparationRecordByUid(uid, "Revoked", ""))
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
