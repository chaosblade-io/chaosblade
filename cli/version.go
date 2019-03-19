package main

import (
	"github.com/spf13/cobra"
	"github.com/chaosblade-io/chaosblade/version"
)

var (
	ver       = "unknown"
	env       = "unknown"
	buildTime = "unknown"
)

type VersionCommand struct {
	baseCommand
}

func (vc *VersionCommand) Init() {
	initVersion()
	vc.command = &cobra.Command{
		Use:     "version",
		Short:   "Version info",
		Long:    "Version info",
		Aliases: []string{"v"},
		Run: func(cmd *cobra.Command, args []string) {
			cmd.Printf("version: %s\n", ver)
			cmd.Printf("build-time: %s\n", buildTime)
			return
		},
	}
}

func initVersion() {
	version.Version.Ver = ver
	version.Version.Env = env
	version.Version.BuildTime = buildTime
}
