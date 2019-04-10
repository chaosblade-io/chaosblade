package main

import (
	"github.com/spf13/cobra"
	"fmt"
	"github.com/chaosblade-io/chaosblade/exec"
	"context"
	"strings"
	"github.com/chaosblade-io/chaosblade/transport"
)

type QueryDiskCommand struct {
	baseCommand
}

const MountPointArg = "mount-point"

func (qdc *QueryDiskCommand) Init() {
	qdc.command = &cobra.Command{
		Use:   "disk device",
		Short: "Query disk information",
		Long:  "Query disk information for chaos experiments of disk",
		Args:  cobra.ExactArgs(1),
		RunE: func(cmd *cobra.Command, args []string) error {
			return qdc.queryDiskInfo(cmd, args[0])
		},
		Example: qdc.queryDiskExample(),
	}
}

func (qdc *QueryDiskCommand) queryDiskExample() string {
	return `blade query disk mount-point`
}

func (qdc *QueryDiskCommand) queryDiskInfo(command *cobra.Command, arg string) error {
	switch arg {
	case MountPointArg:
		response := exec.NewLocalChannel().Run(context.TODO(), "df",
			fmt.Sprintf(`-h | grep -v 'Mounted on' | awk '{print $NF}' | tr '\n' ' '`))
		if !response.Success {
			return response
		}
		disks := response.Result.(string)
		command.Println(transport.ReturnSuccess(strings.Fields(disks)))
	default:
		return fmt.Errorf("the %s argument not found", arg)
	}
	return nil
}
