package main

import (
	"fmt"
	"os"

	"github.com/chaosblade-io/chaosblade/cli/cmd"
)

func main() {
	baseCommand := cmd.CmdInit()
	if err := baseCommand.CobraCmd().Execute(); err != nil {
		_, _ = fmt.Fprintf(os.Stderr, "%s\n", err.Error())
		os.Exit(1)
	}
}
