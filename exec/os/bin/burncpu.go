package main

import (
	"context"
	"flag"
	"fmt"
	"github.com/chaosblade-io/chaosblade/exec"
	"github.com/chaosblade-io/chaosblade/util"
	"os"
	"path"
	"runtime"
	"strings"
	"time"
)

var (
	burnCpuStart, burnCpuStop, burnCpuNohup bool
	burnTimeout                             uint
)

func main() {
	flag.BoolVar(&burnCpuStart, "start", false, "burn cpu")
	flag.UintVar(&burnTimeout, "timeout", 0, "execute timeout")
	flag.BoolVar(&burnCpuStop, "stop", false, "stop burn cpu")
	flag.BoolVar(&burnCpuNohup, "nohup", false, "nohup to run burn cpu")
	flag.Parse()

	if burnTimeout > 0 {
		fmt.Fprint(os.Stderr, fmt.Sprintf("cpu fullload test will stopped in %d seconds\n", burnTimeout))
		go func() {
			time.Sleep(time.Duration(burnTimeout) * time.Second)
			stopBurnCpu()
		}()
	}

	if burnCpuStart {
		startBurnCpu()
	} else if burnCpuStop {
		stopBurnCpu()
	} else if burnCpuNohup {
		burnCpu()
	}
}

func burnCpu() {
	numCPU := runtime.NumCPU()
	runtime.GOMAXPROCS(numCPU)

	for i := 0; i < numCPU; i++ {
		go func() {
			for {
				for i := 0; i < 2147483647; i++ {
				}
				runtime.Gosched()
			}
		}()
	}
	select {} // wait forever
}

const burnCpuBin = "chaos_burncpu"

// startBurnCpu by invoke burnCpuBin with --nohup flag
func startBurnCpu() {
	args := fmt.Sprintf(`%s --nohup > /dev/null 2>&1 &`, path.Join(util.GetProgramPath(), burnCpuBin))
	if burnTimeout > 0 {
		args = fmt.Sprintf(`%s --nohup --timeout %d > /dev/null 2>&1 &`,
			path.Join(util.GetProgramPath(), burnCpuBin), burnTimeout)
	}
	ctx := context.Background()
	response := exec.NewLocalChannel().Run(ctx, "nohup", args)
	if !response.Success {
		printErrAndExit(response.Err)
	}
	time.Sleep(time.Second)
	// query process
	ctx = context.WithValue(ctx, exec.ProcessKey, "nohup")
	pids, _ := exec.GetPidsByProcessName(burnCpuBin, ctx)
	if pids == nil || len(pids) == 0 {
		printErrAndExit(fmt.Sprintf("%s pid not found", burnCpuBin))
	}
}

// stopBurnCpu
func stopBurnCpu() {
	// add grep nohup
	ctx := context.WithValue(context.Background(), exec.ProcessKey, "nohup")
	pids, _ := exec.GetPidsByProcessName(burnCpuBin, ctx)
	if pids == nil || len(pids) == 0 {
		printOutputAndExit("pid not found")
	}
	exec.NewLocalChannel().Run(ctx, "kill", fmt.Sprintf(`-9 %s`, strings.Join(pids, " ")))
}
