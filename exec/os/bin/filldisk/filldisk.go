package main

import (
	"context"
	"flag"
	"fmt"
	"path"
	"strings"

	"github.com/chaosblade-io/chaosblade/exec"
	"github.com/chaosblade-io/chaosblade/exec/os/bin"
	"github.com/chaosblade-io/chaosblade/util"
)

var fillDataFile = "chaos_filldisk.log.dat"
var fillDiskMountPoint, fillDiskSize string
var fillDiskStart, fillDiskStop bool

func main() {
	flag.StringVar(&fillDiskMountPoint, "mount-point", "", "mount point of disk")
	flag.StringVar(&fillDiskSize, "size", "", "fill size")
	flag.BoolVar(&fillDiskStart, "start", false, "start fill or not")
	flag.BoolVar(&fillDiskStop, "stop", false, "stop fill or not")

	flag.Parse()

	if fillDiskStart == fillDiskStop {
		bin.PrintErrAndExit("must specify start or stop operation")
	}
	if fillDiskStart {
		startFill(fillDiskMountPoint, fillDiskSize)
	} else if fillDiskStop {
		stopFill(fillDiskMountPoint)
	} else {
		bin.PrintErrAndExit("less --start or --stop flag")
	}
}

var channel = exec.NewLocalChannel()

func startFill(mountPoint, size string) {
	ctx := context.Background()
	if mountPoint == "" {
		bin.PrintErrAndExit("mount-point flag is empty")
	}
	dataFile := path.Join(mountPoint, fillDataFile)
	response := channel.Run(ctx, "dd", fmt.Sprintf(`if=/dev/zero of=%s bs=1b count=1 iflag=fullblock`, dataFile))
	if !response.Success {
		stopFill(mountPoint)
		bin.PrintErrAndExit(response.Err)
	}
	response = channel.Run(ctx, "nohup",
		fmt.Sprintf(`dd if=/dev/zero of=%s bs=1M count=%s iflag=fullblock >/dev/null 2>&1 &`, dataFile, size))
	if !response.Success {
		stopFill(mountPoint)
		bin.PrintErrAndExit(response.Err)
	}
	bin.PrintOutputAndExit(response.Result.(string))
}

func stopFill(mountPoint string) {
	ctx := context.Background()
	pids, _ := exec.GetPidsByProcessName(fillDataFile, ctx)

	if pids != nil || len(pids) >= 0 {
		channel.Run(ctx, "kill", fmt.Sprintf("-9 %s", strings.Join(pids, " ")))
	}
	fileName := path.Join(mountPoint, fillDataFile)
	if util.IsExist(fileName) {
		response := channel.Run(ctx, "rm", fmt.Sprintf(`-rf %s`, fileName))
		if !response.Success {
			bin.PrintErrAndExit(response.Err)
		}
	}
}
