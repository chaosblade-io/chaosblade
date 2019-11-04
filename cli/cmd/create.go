package cmd

import (
	"github.com/spf13/cobra"
)

// CreateCommand for create experiment
type CreateCommand struct {
	baseCommand
}

const UidFlag = "uid"

var uid string

func (cc *CreateCommand) Init() {
	cc.command = &cobra.Command{
		Use:     "create",
		Short:   "Create a chaos engineering experiment",
		Long:    "Create a chaos engineering experiment",
		Aliases: []string{"c"},
		Example: createExample(),
	}
	flags := cc.command.PersistentFlags()
	flags.StringVar(&uid, UidFlag, "", "Set Uid for the experiment, adapt to docker")
}

func createExample() string {
	return `blade create cpu load --cpu-percent 60`
}
