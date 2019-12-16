package cmd

import logf "sigs.k8s.io/controller-runtime/pkg/runtime/log"

var log = logf.Log.WithName("cmd")

func CmdInit() *baseCommand {
	cli := NewCli()
	baseCmd := &baseCommand{
		command: cli.rootCmd,
	}
	// add version command
	baseCmd.AddCommand(&VersionCommand{})
	// add prepare command
	prepareCommand := &PrepareCommand{}
	baseCmd.AddCommand(prepareCommand)
	prepareCommand.AddCommand(&PrepareJvmCommand{})
	prepareCommand.AddCommand(&PrepareCPlusCommand{})

	// add revoke command
	baseCmd.AddCommand(&RevokeCommand{})

	// add create command
	createCommand := &CreateCommand{}
	baseCmd.AddCommand(createCommand)

	// add destroy command
	destroyCommand := &DestroyCommand{}
	baseCmd.AddCommand(destroyCommand)

	// add status command
	baseCmd.AddCommand(&StatusCommand{})

	// add query command
	queryCommand := &QueryCommand{}
	baseCmd.AddCommand(queryCommand)
	queryCommand.AddCommand(&QueryDiskCommand{})
	queryCommand.AddCommand(&QueryNetworkCommand{})
	queryCommand.AddCommand(&QueryJvmCommand{})
	queryCommand.AddCommand(&QueryK8sCommand{})

	// add server command
	serverCommand := &ServerCommand{}
	baseCmd.AddCommand(serverCommand)
	serverCommand.AddCommand(&StartServerCommand{})
	serverCommand.AddCommand(&StopServerCommand{})

	return baseCmd
}
