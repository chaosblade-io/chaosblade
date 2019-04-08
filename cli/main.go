package main

import (
	"fmt"
	"os"
)

func main() {
	cli := NewCli()
	baseCmd := &baseCommand{
		command: cli.rootCmd,
		debug:   cli.Debug,
	}
	// add version command
	baseCmd.AddCommand(&VersionCommand{})
	// add prepare command
	prepareCommand := &PrepareCommand{}
	baseCmd.AddCommand(prepareCommand)
	prepareCommand.AddCommand(&PrepareJvmCommand{})
	// add revoke command
	baseCmd.AddCommand(&RevokeCommand{})

	// add create command
	createCommand := &CreateCommand{}
	baseCmd.AddCommand(createCommand)

	// add exp command
	expCommand := NewExpCommand()
	expCommand.AddCommandTo(createCommand)

	// add destroy command
	baseCmd.AddCommand(&DestroyCommand{exp: expCommand})

	// add status command
	baseCmd.AddCommand(&StatusCommand{exp: expCommand})

	// add query command
	queryCommand := &QueryCommand{}
	baseCmd.AddCommand(queryCommand)
	queryCommand.AddCommand(&QueryDiskCommand{})
	queryCommand.AddCommand(&QueryNetworkCommand{})

	if err := cli.Run(); err != nil {
		fmt.Fprintf(os.Stderr, "%s\n", err.Error())
		os.Exit(1)
	}
}
