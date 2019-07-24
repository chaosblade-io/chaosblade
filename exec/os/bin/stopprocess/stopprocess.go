package main

import (
	"context"
	"flag"
	"fmt"
	"strings"

	"github.com/chaosblade-io/chaosblade/exec"
	"github.com/chaosblade-io/chaosblade/exec/os/bin"
)

var stopProcessName string
var stopProcessInCmd string

var startFakeDeath, stopFakeDeath bool

func main() {
	flag.StringVar(&stopProcessName, "process", "", "process name")
	flag.StringVar(&stopProcessInCmd, "process-cmd", "", "process in command")
	flag.BoolVar(&startFakeDeath, "start", false, "start process fake death")
	flag.BoolVar(&stopFakeDeath, "stop", false, "recover process fake death")
	flag.Parse()

	if startFakeDeath == stopFakeDeath {
		bin.PrintErrAndExit("must add --start or --stop flag")
	}

	if startFakeDeath {
		doStopProcess(stopProcessName, stopProcessInCmd)
	} else if stopFakeDeath {
		doRecoverProcess(stopProcessName, stopProcessInCmd)
	} else {
		bin.PrintErrAndExit("less --start or --stop flag")
	}
}

func doStopProcess(process, processCmd string) {
	var pids []string
	var err error
	var ctx = context.Background()
	if process != "" {
		pids, err = exec.GetPidsByProcessName(process, ctx)
		if err != nil {
			bin.PrintErrAndExit(err.Error())
		}
		stopProcessName = process
	} else if processCmd != "" {
		pids, err = exec.GetPidsByProcessCmdName(processCmd, ctx)
		if err != nil {
			bin.PrintErrAndExit(err.Error())
		}
		stopProcessName = processCmd
	}

	if pids == nil || len(pids) == 0 {
		bin.PrintErrAndExit(fmt.Sprintf("%s process not found", stopProcessName))
	}
	args := fmt.Sprintf("-19 %s", strings.Join(pids, " "))
	response := exec.NewLocalChannel().Run(ctx, "kill", args)
	if !response.Success {
		bin.PrintErrAndExit(response.Err)
	}
	bin.PrintOutputAndExit(response.Result.(string))
}

func doRecoverProcess(process, processCmd string) {
	var pids []string
	var err error
	var ctx = context.Background()
	if process != "" {
		pids, err = exec.GetPidsByProcessName(process, ctx)
		if err != nil {
			bin.PrintErrAndExit(err.Error())
		}
		stopProcessName = process
	} else if processCmd != "" {
		pids, err = exec.GetPidsByProcessCmdName(processCmd, ctx)
		if err != nil {
			bin.PrintErrAndExit(err.Error())
		}
		stopProcessName = processCmd
	}

	if pids == nil || len(pids) == 0 {
		bin.PrintErrAndExit(fmt.Sprintf("%s process not found", stopProcessName))
	}
	response := exec.NewLocalChannel().Run(ctx, "kill", fmt.Sprintf("-18 %s", strings.Join(pids, " ")))
	if !response.Success {
		bin.PrintErrAndExit(response.Err)
	}
	bin.PrintOutputAndExit(response.Result.(string))
}
