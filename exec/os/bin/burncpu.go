package main

import (
	"context"
	"flag"
	"fmt"
	"github.com/chaosblade-io/chaosblade/exec"
	"github.com/chaosblade-io/chaosblade/util"
	"path"
	"runtime"
	"strings"
	"time"
)

var (
	burnCpuStart, burnCpuStop, burnCpuNohup bool
	numCPU                                  int
)

func main() {
	flag.BoolVar(&burnCpuStart, "start", false, "burn cpu")
	flag.BoolVar(&burnCpuStop, "stop", false, "stop burn cpu")
	flag.BoolVar(&burnCpuNohup, "nohup", false, "nohup to run burn cpu")
	flag.IntVar(&numCPU, "numcpu", runtime.NumCPU(), "number of cpus")
	flag.Parse()

	if numCPU <= 0 || numCPU > runtime.NumCPU() {
		numCPU = runtime.NumCPU()
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
	args := fmt.Sprintf(`%s --nohup --numcpu %d > /dev/null 2>&1 &`,
		path.Join(util.GetProgramPath(), burnCpuBin), numCPU)
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
