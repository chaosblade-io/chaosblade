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

var burnIODevice, burnIOSize, burnIOCount string
var burnIORead, burnIOWrite, burnIOStart, burnIOStop, burnIONohup bool

func main() {
	flag.StringVar(&burnIODevice, "device", "", "disk device")
	flag.StringVar(&burnIOSize, "size", "", "block size")
	flag.StringVar(&burnIOCount, "count", "", "block count")
	flag.BoolVar(&burnIOWrite, "write", false, "write io")
	flag.BoolVar(&burnIORead, "read", false, "read io")
	flag.BoolVar(&burnIOStart, "start", false, "start burn io")
	flag.BoolVar(&burnIOStop, "stop", false, "stop burn io")
	flag.BoolVar(&burnIONohup, "nohup", false, "start by nohup")

	flag.Parse()

	if burnIOStart {
		device, err := getFileSystem(burnIODevice)
		if err != nil || device == "" {
			printErrAndExit(fmt.Sprintf("cannot find mount device, %s", burnIODevice))
		}
		startBurnIO(device, burnIOSize, burnIOCount, burnIORead, burnIOWrite)
	} else if burnIOStop {
		stopBurnIO()
	} else if burnIONohup {
		if burnIORead {
			go burnRead(burnIODevice, burnIOSize, burnIOCount)
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
func startBurnIO(device, size, count string, read, write bool) {
	channel := exec.NewLocalChannel()
	ctx := context.Background()
	response := channel.Run(ctx, "nohup",
		fmt.Sprintf(`%s --device %s --size %s --count %s --read=%t --write=%t --nohup=true > %s 2>&1 &`,
			path.Join(util.GetProgramPath(), burnIOBin), device, size, count, read, write, logFile))
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
func burnRead(device, size, count string) {
	for {
		args := fmt.Sprintf(`if=%s of=/dev/null bs=%sM count=%s iflag=dsync,direct,fullblock`, device, size, count)
		response := exec.NewLocalChannel().Run(context.Background(), "dd", args)
		if !response.Success {
			printAndExitWithErrPrefix(response.Err)
		}
	}
}

// get fileSystem by mount point
func getFileSystem(mountOn string) (string, error) {
	response := exec.NewLocalChannel().Run(context.Background(), "mount", fmt.Sprintf(` | grep "on %s " | awk '{print $1}'`, mountOn))
	if response.Success {
		fileSystem := response.Result.(string)
		return strings.TrimSpace(fileSystem), nil
	}
	return "", fmt.Errorf(response.Err)
}
