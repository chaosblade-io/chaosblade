package main

import (
	"context"
	"flag"
	"fmt"
	"strings"

	"github.com/chaosblade-io/chaosblade/exec"
)

var killProcessName string
var killProcessInCmd string

func main() {
	flag.StringVar(&killProcessName, "process", "", "process name")
	flag.StringVar(&killProcessInCmd, "process-cmd", "", "process in command")

	flag.Parse()

	killProcess(killProcessName, killProcessInCmd)
}

func killProcess(process, processCmd string) {
	var pids []string
	var err error
	var ctx = context.Background()
	if process != "" {
		pids, err = exec.GetPidsByProcessName(process, ctx)
		if err != nil {
			PrintErrAndExit(err.Error())
		}
		killProcessName = process
	} else if processCmd != "" {
		pids, err = exec.GetPidsByProcessCmdName(processCmd, ctx)
		if err != nil {
			PrintErrAndExit(err.Error())
		}
		killProcessName = processCmd
	}

	if pids == nil || len(pids) == 0 {
		PrintErrAndExit(fmt.Sprintf("%s process not found", killProcessName))
	}
	response := exec.NewLocalChannel().Run(ctx, "kill", fmt.Sprintf("-9 %s", strings.Join(pids, " ")))
	if !response.Success {
		PrintErrAndExit(response.Err)
	}
	PrintOutputAndExit(response.Result.(string))
}
