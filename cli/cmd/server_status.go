package cmd

import (
	"context"
	"fmt"
	"strings"

	"github.com/chaosblade-io/chaosblade-spec-go/channel"
	"github.com/chaosblade-io/chaosblade-spec-go/spec"
	"github.com/spf13/cobra"
)

type StatusServerCommand struct {
	baseCommand
}

func (ssc *StatusServerCommand) Init() {
	ssc.command = &cobra.Command{
		Use:     "status",
		Short:   "Prints out the status of blade server",
		Long:    "Prints out the status of blade server",
		Aliases: []string{"s"},
		RunE: func(cmd *cobra.Command, args []string) error {
			return ssc.run(cmd, args)
		},
		Example: statusServerExample(),
	}
}

func (ssc *StatusServerCommand) run(cmd *cobra.Command, args []string) error {
	// check if the process named `blade server --start` exists or not
	pids, err := channel.GetPidsByProcessName(startServerKey, context.TODO())
	if err != nil {
		return spec.ReturnFail(spec.Code[spec.ServerError], err.Error())
	}
	if len(pids) != 0 {
		data := map[string]string{
			"status": "up",
			"port":   "",
		}
		response := channel.NewLocalChannel().Run(context.TODO(), "ps", fmt.Sprintf("-p %s | grep port", strings.Join(pids, " ")))
		fmtStrs := strings.Split(strings.Replace(fmt.Sprintf("%v", response.Result), "\n", "", -1), " ")
		for i, p := range fmtStrs {
			if p == "--port" {
				data["port"] = fmtStrs[i+1]
			}
		}
		response = spec.ReturnSuccess(data)
		ssc.command.Println(response.Print())
	} else {
		return spec.ReturnFail(spec.Code[spec.ServerError], "down")
	}
	return nil
}

func statusServerExample() string {
	return `blade server status`
}
