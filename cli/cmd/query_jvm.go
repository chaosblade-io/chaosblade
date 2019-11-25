package cmd

import (
	"fmt"

	"github.com/spf13/cobra"

	"github.com/chaosblade-io/chaosblade/exec/jvm"
)

type QueryJvmCommand struct {
	baseCommand
}

func (qjc *QueryJvmCommand) Init() {
	qjc.command = &cobra.Command{
		Use:   "jvm <UID>",
		Short: "Query hit counts of the specify experiment",
		Long:  "Query hit counts of the specify experiment",
		Args:  cobra.ExactArgs(1),
		RunE: func(cmd *cobra.Command, args []string) error {
			return qjc.queryJvmExpStatus(cmd, args[0])
		},
		Example: qjc.queryJvmExample(),
	}
}

func (qjc *QueryJvmCommand) queryJvmExample() string {
	return `blade query jvm 29c3f9dab4abbc79`
}

// queryJvmExpStatus by uid
func (qjc *QueryJvmCommand) queryJvmExpStatus(command *cobra.Command, arg string) error {
	response := jvm.NewExecutor().QueryStatus(arg)
	if response.Success {
		command.Println(response.Print())
	} else {
		return fmt.Errorf(response.Error())
	}
	return nil
}
