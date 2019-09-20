package main

import (
	"fmt"
	"os"

	"github.com/chaosblade-io/chaosblade/cli/cmd"
)

var (
	ver       = "unknown"
	env       = "unknown"
	buildTime = "unknown"
)

func main() {
	baseCommand := cmd.CmdInit(ver, env, buildTime)
	if err := baseCommand.CobraCmd().Execute(); err != nil {
		_, _ = fmt.Fprintf(os.Stderr, "%s\n", err.Error())
		os.Exit(1)
	}
}
