package cmd

import (
	"fmt"

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
			return spec.ReturnFail(spec.Code[spec.IllegalCommand],
				fmt.Sprintf("less TARGE to query"))
		},
		Example: qc.queryExample(),
	}
}

func (qc *QueryCommand) queryExample() string {
	return `query network interface`
}
