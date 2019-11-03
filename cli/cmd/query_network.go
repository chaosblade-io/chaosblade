package cmd

import (
	"fmt"
	"net"
	"strings"

	"github.com/chaosblade-io/chaosblade-spec-go/spec"
	"github.com/spf13/cobra"
)

type QueryNetworkCommand struct {
	baseCommand
}

const InterfaceArg = "interface"

func (qnc *QueryNetworkCommand) Init() {
	qnc.command = &cobra.Command{
		Use:     "network interface",
		Aliases: []string{"net"},
		Short:   "Query network information",
		Long:    "Query network information for chaos experiments of network",
		Args:    cobra.ExactArgs(1),
		RunE: func(cmd *cobra.Command, args []string) error {
			return qnc.queryNetworkInfo(cmd, args[0])
		},
		Example: qnc.queryNetworkExample(),
	}
}

func (qnc *QueryNetworkCommand) queryNetworkExample() string {
	return `blade query network interface`
}

func (qnc *QueryNetworkCommand) queryNetworkInfo(command *cobra.Command, arg string) error {
	switch arg {
	case InterfaceArg:
		interfaces, err := net.Interfaces()
		if err != nil {
			return err
		}
		names := make([]string, 0)
		for _, i := range interfaces {
			if strings.Contains(i.Flags.String(), "up") {
				names = append(names, i.Name)
			}
		}
		command.Println(spec.ReturnSuccess(names))
	default:
		return fmt.Errorf("the %s argument not found", arg)
	}
	return nil
}
