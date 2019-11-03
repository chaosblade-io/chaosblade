package cmd

import (
	"context"
	"fmt"
	"strings"

	"github.com/chaosblade-io/chaosblade-spec-go/spec"
	"github.com/spf13/cobra"
	"github.com/chaosblade-io/chaosblade-spec-go/channel"
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
		response := channel.NewLocalChannel().Run(context.TODO(), "df",
			fmt.Sprintf(`-h | awk 'NR!=1 {print $1","$NF}' | tr '\n' ' '`))
		if !response.Success {
			return response
		}
		disks := response.Result.(string)
		fields := strings.Fields(disks)
		var result = make([]string, 0)
		for _, disk := range fields {
			// TODO Check the file system prefix, but should check the file system type
			if strings.HasPrefix(disk, "/") {
				arr := strings.Split(disk, ",")
				if len(arr) < 2 {
					continue
				}
				result = append(result, arr[1])
			}
		}
		command.Println(spec.ReturnSuccess(result))
	default:
		return fmt.Errorf("the %s argument not found", arg)
	}
	return nil
}
