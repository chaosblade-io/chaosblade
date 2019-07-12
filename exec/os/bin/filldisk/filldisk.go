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
var fillDiskMountPoint, fillDiskBlockCount string
var fillDiskStart, fillDiskStop bool

func main() {
	flag.StringVar(&fillDiskMountPoint, "mount-point", "", "mount point of disk")
	flag.StringVar(&fillDiskBlockCount, "count", "", "number of blocks to fill fisk(default block size=1M)")
	flag.BoolVar(&fillDiskStart, "start", false, "start fill disk")
	flag.BoolVar(&fillDiskStop, "stop", false, "stop fill disk")

	flag.Parse()

	if !fillDiskStart && !fillDiskStop {
		bin.PrintErrAndExit("must specify start or stop operation")
	}
	
	if fillDiskStart {
		startFill(fillDiskMountPoint, fillDiskBlockCount)
	} else if fillDiskStop {
		stopFill(fillDiskMountPoint)
	} else {
		bin.PrintErrAndExit("less --start or --stop flag")
	}
}

func startFill(mountPoint, count string) {
	channel := exec.NewLocalChannel()
	ctx := context.Background()
	if mountPoint == "" {
		bin.PrintErrAndExit("mount-point flag is empty")
	}
	dataFile := path.Join(mountPoint, fillDataFile)
	response := channel.Run(ctx, "nohup",
		fmt.Sprintf(`dd if=/dev/zero of=%s bs=1M count=%s iflag=fullblock >/dev/null 2>&1 &`, dataFile, count))
	if !response.Success {
		stopFill(mountPoint)
		bin.PrintErrAndExit(response.Err)
	}
	bin.PrintOutputAndExit(response.Result.(string))
}

func stopFill(mountPoint string) {
	channel := exec.NewLocalChannel()
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
