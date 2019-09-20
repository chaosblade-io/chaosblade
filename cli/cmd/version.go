package cmd

import (
	"github.com/chaosblade-io/chaosblade/version"
	"github.com/spf13/cobra"
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
			cmd.Printf("version: %s\n", version.Version.Ver)
			cmd.Printf("env: %s\n", version.Version.Env)
			cmd.Printf("build-time: %s\n", version.Version.BuildTime)
			return
		},
	}
}
