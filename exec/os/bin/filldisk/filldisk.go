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
var fillDiskSize, fillDiskDirectory string
var fillDiskStart, fillDiskStop bool

func main() {
	flag.StringVar(&fillDiskDirectory, "directory", "", "the directory where the disk is populated")
	flag.StringVar(&fillDiskSize, "size", "", "fill size")
	flag.BoolVar(&fillDiskStart, "start", false, "start fill or not")
	flag.BoolVar(&fillDiskStop, "stop", false, "stop fill or not")

	flag.Parse()

	if fillDiskStart == fillDiskStop {
		bin.PrintErrAndExit("must specify start or stop operation")
	}
	if fillDiskStart {
		startFill(fillDiskDirectory, fillDiskSize)
	} else if fillDiskStop {
		stopFill(fillDiskDirectory)
	} else {
		bin.PrintErrAndExit("less --start or --stop flag")
	}
}

var channel = exec.NewLocalChannel()

func startFill(directory, size string) {
	ctx := context.Background()
	if directory == "" {
		bin.PrintErrAndExit("--directory flag value is empty")
	}
	dataFile := path.Join(directory, fillDataFile)

	// Some normal filesystems (ext4, xfs, btrfs and ocfs2) tack quick works
	if exec.IsCommandAvailable("fallocate") {
		response := channel.Run(ctx, "fallocate", fmt.Sprintf(`-l %sM %s`, size, dataFile))
		if !response.Success {
			stopFill(directory)
			bin.PrintErrAndExit(response.Err)
		}
		bin.PrintOutputAndExit(response.Result.(string))
	}

	response := channel.Run(ctx, "dd", fmt.Sprintf(`if=/dev/zero of=%s bs=1b count=1 iflag=fullblock`, dataFile))
	if !response.Success {
		stopFill(directory)
		bin.PrintErrAndExit(response.Err)
	}
	response = channel.Run(ctx, "nohup",
		fmt.Sprintf(`dd if=/dev/zero of=%s bs=1M count=%s iflag=fullblock >/dev/null 2>&1 &`, dataFile, size))
	if !response.Success {
		stopFill(directory)
		bin.PrintErrAndExit(response.Err)
	}
	bin.PrintOutputAndExit(response.Result.(string))
}

func stopFill(directory string) {
	ctx := context.Background()
	pids, _ := exec.GetPidsByProcessName(fillDataFile, ctx)

	if pids != nil || len(pids) >= 0 {
		channel.Run(ctx, "kill", fmt.Sprintf("-9 %s", strings.Join(pids, " ")))
	}
	fileName := path.Join(directory, fillDataFile)
	if util.IsExist(fileName) {
		response := channel.Run(ctx, "rm", fmt.Sprintf(`-rf %s`, fileName))
		if !response.Success {
			bin.PrintErrAndExit(response.Err)
		}
	}
}
