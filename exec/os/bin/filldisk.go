package main

import (
	"fmt"
	"strings"
	"github.com/chaosblade-io/chaosblade/exec"
	"flag"
	"context"
)

var fillDataFile = "chaos_filldisk.log.dat"
var fillDiskDevice, fillDiskSize string
var fillDiskStart, fillDiskStop bool

func main() {
	flag.StringVar(&fillDiskDevice, "device", "", "mount device")
	flag.StringVar(&fillDiskSize, "size", "", "fill size")
	flag.BoolVar(&fillDiskStart, "start", false, "start fill or not")
	flag.BoolVar(&fillDiskStop, "stop", false, "stop fill or not")

	flag.Parse()

	if fillDiskStart == fillDiskStop {
		printErrAndExit("must specify start or stop operation")
	}
	if fillDiskStart {
		startFill(fillDiskDevice, fillDiskSize)
	} else if fillDiskStop {
		stopFill(fillDiskDevice)
	} else {
		printErrAndExit("less --start or --stop flag")
	}
}

func startFill(device, size string) {
	channel := exec.NewLocalChannel()
	ctx := context.Background()
	response := channel.Run(ctx, "df", fmt.Sprintf(`-h %s | grep -v 'Mounted on' | awk '{print $NF}'`, device))
	if !response.Success {
		printErrAndExit(response.Err)
	}
	path := strings.TrimSpace(response.Result.(string))
	if len(path) == 0 {
		printErrAndExit("cannot find disk device")
	}
	if path[len(path)-1:] != "/" {
		path = path + "/"
	}
	dataFile := fmt.Sprintf("%s%s", path, fillDataFile)
	response = channel.Run(ctx, "dd", fmt.Sprintf(`if=/dev/zero of=%s bs=1b count=1 iflag=fullblock`, dataFile))
	if !response.Success {
		stopFill(device)
		printErrAndExit(response.Err)
	}
	response = channel.Run(ctx, "nohup",
		fmt.Sprintf(`dd if=/dev/zero of=%s bs=1M count=%s iflag=fullblock >/dev/null 2>&1 &`, dataFile, size))
	if !response.Success {
		stopFill(device)
		printErrAndExit(response.Err)
	}
	printOutputAndExit(response.Result.(string))
}

func stopFill(device string) {
	channel := exec.NewLocalChannel()
	ctx := context.Background()
	pids, _ := exec.GetPidsByProcessName(fillDataFile, ctx)

	if pids != nil || len(pids) >= 0 {
		channel.Run(ctx, "kill", fmt.Sprintf("-9 %s", strings.Join(pids, " ")))
	}
	channel.Run(ctx, "rm", fmt.Sprintf(`-rf %s%s`, device, fillDataFile))
}
