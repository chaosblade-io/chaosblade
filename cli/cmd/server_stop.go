package cmd

import (
	"github.com/spf13/cobra"
	"fmt"
	"github.com/chaosblade-io/chaosblade/exec"
	"strings"
	"context"
	"github.com/chaosblade-io/chaosblade/transport"
	"github.com/sirupsen/logrus"
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
	pids, err := exec.GetPidsByProcessName(startServerKey, context.TODO())
	if err != nil {
		return transport.ReturnFail(transport.Code[transport.ServerError], err.Error())
	}
	if pids == nil || len(pids) == 0 {
		logrus.Infof("the blade server process not found, so return success for stop operation")
		cmd.Printf(transport.ReturnSuccess("success").Print())
		return nil
	}
	response := exec.NewLocalChannel().Run(context.TODO(), "kill", fmt.Sprintf("-9 %s", strings.Join(pids, " ")))
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
