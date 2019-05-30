package main

import (
	"flag"
	"fmt"
	"github.com/chaosblade-io/chaosblade/exec"
	"context"
	"strings"
	"time"
	"path"
	"github.com/chaosblade-io/chaosblade/util"
)

var burnIOMountPoint, burnIOFileSystem, burnIOSize, burnIOCount string
var burnIORead, burnIOWrite, burnIOStart, burnIOStop, burnIONohup bool

func main() {
	// Filesystem      Size  Used Avail Use% Mounted on
	///dev/vda1        40G  9.5G   28G  26% /
	//  mount-point value is /, file-system value is /dev/vda1
	flag.StringVar(&burnIOMountPoint, "mount-point", "", "mount point of disk")
	flag.StringVar(&burnIOFileSystem, "file-system", "", "file system of disk")
	flag.StringVar(&burnIOSize, "size", "", "block size")
	flag.StringVar(&burnIOCount, "count", "", "block count")
	flag.BoolVar(&burnIOWrite, "write", false, "write io")
	flag.BoolVar(&burnIORead, "read", false, "read io")
	flag.BoolVar(&burnIOStart, "start", false, "start burn io")
	flag.BoolVar(&burnIOStop, "stop", false, "stop burn io")
	flag.BoolVar(&burnIONohup, "nohup", false, "start by nohup")

	flag.Parse()

	if burnIOStart {
		fileSystem, err := getFileSystem(burnIOMountPoint)
		if err != nil || fileSystem == "" {
			printErrAndExit(fmt.Sprintf("cannot find mount point, %s", burnIOMountPoint))
		}
		startBurnIO(fileSystem, burnIOSize, burnIOCount, burnIORead, burnIOWrite)
	} else if burnIOStop {
		stopBurnIO()
	} else if burnIONohup {
		if burnIORead {
			go burnRead(burnIOFileSystem, burnIOSize, burnIOCount)
		}
		if burnIOWrite {
			go burnWrite(burnIOSize, burnIOCount)
		}
		select {}
	} else {
		printErrAndExit("less --start or --stop flag")
	}
}

var tmpDataFile = "/tmp/chaos_burnio.log.dat"
var logFile = "/tmp/chaos_burnio.log"
var burnIOBin = "chaos_burnio"

// start burn io
func startBurnIO(fileSystem, size, count string, read, write bool) {
	channel := exec.NewLocalChannel()
	ctx := context.Background()
	response := channel.Run(ctx, "nohup",
		fmt.Sprintf(`%s --file-system %s --size %s --count %s --read=%t --write=%t --nohup=true > %s 2>&1 &`,
			path.Join(util.GetProgramPath(), burnIOBin), fileSystem, size, count, read, write, logFile))
	if !response.Success {
		stopBurnIO()
		printErrAndExit(response.Err)
	}
	// check
	time.Sleep(time.Second)
	response = channel.Run(ctx, "grep", fmt.Sprintf("%s %s", ErrPrefix, logFile))
	if response.Success {
		errMsg := strings.TrimSpace(response.Result.(string))
		if errMsg != "" {
			stopBurnIO()
			printErrAndExit(errMsg)
		}
	}
	printOutputAndExit("success")
}

var taskName = []string{"if=/dev/zero", "of=/dev/null"}

// stop burn io,  no need to add os.Exit
func stopBurnIO() {
	channel := exec.NewLocalChannel()
	ctx := context.Background()
	for _, name := range taskName {
		pids, _ := exec.GetPidsByProcessName(name, ctx)
		if pids == nil || len(pids) == 0 {
			continue
		}
		channel.Run(ctx, "kill", fmt.Sprintf("-9 %s", strings.Join(pids, " ")))
	}
	channel.Run(ctx, "rm", fmt.Sprintf("-rf %s*", logFile))
}

// write burn
func burnWrite(size, count string) {
	for {
		args := fmt.Sprintf(`if=/dev/zero of=%s bs=%sM count=%s oflag=dsync`, tmpDataFile, size, count)
		response := exec.NewLocalChannel().Run(context.Background(), "dd", args)
		exec.NewLocalChannel().Run(context.Background(), "rm", fmt.Sprintf(`-rf %s`, tmpDataFile))
		if !response.Success {
			printAndExitWithErrPrefix(response.Err)
		}
	}
}

// read burn
func burnRead(fileSystem, size, count string) {
	for {
		// "if" arg in dd command is file system value, but "of" arg value is related to mount point
		args := fmt.Sprintf(`if=%s of=/dev/null bs=%sM count=%s iflag=dsync,direct,fullblock`, fileSystem, size, count)
		response := exec.NewLocalChannel().Run(context.Background(), "dd", args)
		if !response.Success {
			printAndExitWithErrPrefix(fmt.Sprintf("The file system named %s is not supported or %s", fileSystem, response.Err))
		}
	}
}

// get fileSystem by mount point
func getFileSystem(mountPoint string) (string, error) {
	response := exec.NewLocalChannel().Run(context.Background(), "mount", fmt.Sprintf(` | grep "on %s " | awk '{print $1}'`, mountPoint))
	if response.Success {
		fileSystem := response.Result.(string)
		return strings.TrimSpace(fileSystem), nil
	}
	return "", fmt.Errorf(response.Err)
}
