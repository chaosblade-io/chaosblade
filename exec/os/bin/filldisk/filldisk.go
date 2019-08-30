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

const diskFillErrorMessage = "No space left on device"

func main() {
	flag.StringVar(&fillDiskDirectory, "directory", "", "the directory where the disk is populated")
	flag.StringVar(&fillDiskSize, "size", "", "fill size, unit is M")
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
		fillDiskByFallocate(ctx, size, dataFile)
	}
	// If execute fallocate command failed, use dd command to retry.
	fillDiskByDD(ctx, dataFile, directory, size)
}

func fillDiskByFallocate(ctx context.Context, size string, dataFile string) {
	response := channel.Run(ctx, "fallocate", fmt.Sprintf(`-l %sM %s`, size, dataFile))
	if response.Success {
		bin.PrintOutputAndExit(response.Result.(string))
	}
	// Need to judge that the disk is full or not. If the disk is full, return success
	if strings.Contains(response.Err, diskFillErrorMessage) {
		bin.PrintOutputAndExit(fmt.Sprintf("success because of %s", diskFillErrorMessage))
	}
}

func fillDiskByDD(ctx context.Context, dataFile string, directory string, size string) {
	// Because of filling disk slowly using dd, so execute dd with 1b size first to test the command.
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

// stopFill contains kill the filldisk process and delete the temp file actions
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
