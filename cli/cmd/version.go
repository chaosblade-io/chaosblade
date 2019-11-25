package cmd

import (
	"github.com/spf13/cobra"

	"github.com/chaosblade-io/chaosblade/version"
)

type VersionCommand struct {
	baseCommand
}

func (vc *VersionCommand) Init() {
	vc.command = &cobra.Command{
		Use:     "version",
		Short:   "Print version info",
		Long:    "Print version info",
		Aliases: []string{"v"},
		Run: func(cmd *cobra.Command, args []string) {
			cmd.Printf("version: %s\n", version.Ver)
			cmd.Printf("env: %s\n", version.Env)
			cmd.Printf("build-time: %s\n", version.BuildTime)
			return
		},
	}
}
