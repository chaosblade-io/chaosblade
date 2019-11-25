package cmd

import (
	"context"
	"fmt"
	"strings"

	"github.com/chaosblade-io/chaosblade-spec-go/channel"
	"github.com/chaosblade-io/chaosblade-spec-go/spec"
	"github.com/sirupsen/logrus"
	"github.com/spf13/cobra"
)

type StopServerCommand struct {
	baseCommand
}

func (ssc *StopServerCommand) Init() {
	ssc.command = &cobra.Command{
		Use:   "stop",
		Short: "Stop server mode, closes web services",
		Long:  "Stop server mode, closes web services",
		RunE: func(cmd *cobra.Command, args []string) error {
			return ssc.run(cmd, args)
		},
		Example: closeServerExample(),
	}
}

func (ssc *StopServerCommand) run(cmd *cobra.Command, args []string) error {
	pids, err := channel.GetPidsByProcessName(startServerKey, context.TODO())
	if err != nil {
		return spec.ReturnFail(spec.Code[spec.ServerError], err.Error())
	}
	if pids == nil || len(pids) == 0 {
		logrus.Infof("the blade server process not found, so return success for stop operation")
		cmd.Printf(spec.ReturnSuccess("success").Print())
		return nil
	}
	response := channel.NewLocalChannel().Run(context.TODO(), "kill", fmt.Sprintf("-9 %s", strings.Join(pids, " ")))
	if !response.Success {
		return response
	}
	response.Result = fmt.Sprintf("pid is %s", strings.Join(pids, " "))
	cmd.Printf(response.Print())
	return nil
}

func closeServerExample() string {
	return `blade server stop`
}
