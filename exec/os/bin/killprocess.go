package main

import (
	"github.com/chaosblade-io/chaosblade/exec"
	"fmt"
	"strings"
	"flag"
	"context"
)

var processName string
var processInCmd string

func main() {
	flag.StringVar(&processName, "process", "", "process name")
	flag.StringVar(&processInCmd, "process-cmd", "", "process in command")

	flag.Parse()

	killProcess(processName, processInCmd)
}

func killProcess(process, processCmd string) {
	var pids []string
	var err error
	var ctx = context.Background()
	if process != "" {
		pids, err = exec.GetPidsByProcessName(process, ctx)
		if err != nil {
			printErrAndExit(err.Error())
		}
		processName = process
	} else if processCmd != "" {
		pids, err = exec.GetPidsByProcessCmdName(processCmd, ctx)
		if err != nil {
			printErrAndExit(err.Error())
		}
		processName = processCmd
	}

	if pids == nil || len(pids) == 0 {
		printErrAndExit(fmt.Sprintf("%s process not found", processName))
	}
	response := exec.NewLocalChannel().Run(ctx, "kill", fmt.Sprintf("-9 %s", strings.Join(pids, " ")))
	if !response.Success {
		printErrAndExit(response.Err)
	}
	printOutputAndExit(response.Result.(string))
}
