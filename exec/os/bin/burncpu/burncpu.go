package main

import (
	"context"
	"flag"
	"fmt"
	"os"
	"path"
	"runtime"
	"strings"
	"time"

	"strconv"

	"github.com/chaosblade-io/chaosblade/exec"
	"github.com/chaosblade-io/chaosblade/exec/os/bin"
	"github.com/chaosblade-io/chaosblade/util"
	"github.com/shirou/gopsutil/cpu"
	"github.com/shirou/gopsutil/process"
)

var (
	burnCpuStart, burnCpuStop, burnCpuNohup bool
	cpuCount, cpuPercent                    int
	cpuList                                 string
	cpuProcessor                            string
)

func main() {
	flag.BoolVar(&burnCpuStart, "start", false, "start burn cpu")
	flag.BoolVar(&burnCpuStop, "stop", false, "stop burn cpu")
	flag.StringVar(&cpuList, "cpu-list", "", "CPUs in which to allow burning (1,3)")
	flag.BoolVar(&burnCpuNohup, "nohup", false, "nohup to run burn cpu")
	flag.IntVar(&cpuCount, "cpu-count", runtime.NumCPU(), "number of cpus")
	flag.IntVar(&cpuPercent, "cpu-percent", 100, "percent of burn-cpu")
	flag.StringVar(&cpuProcessor, "cpu-processor", "0", "only used for identifying process of cpu burn")
	flag.Parse()

	if cpuCount <= 0 || cpuCount > runtime.NumCPU() {
		cpuCount = runtime.NumCPU()
	}

	if burnCpuStart {
		startBurnCpu()
	} else if burnCpuStop {
		if success, errs := stopBurnCpuFunc(); !success {
			bin.PrintErrAndExit(errs)
		}
	} else if burnCpuNohup {
		burnCpu()
	} else {
		bin.PrintErrAndExit("less --start or --stop flag")
	}
}

func burnCpu() {

	runtime.GOMAXPROCS(cpuCount)

	var totalCpuPercent []float64
	var curProcess *process.Process
	var curCpuPercent float64
	var err error

	totalCpuPercent, err = cpu.Percent(time.Second, false)
	if err != nil {
		bin.PrintErrAndExit(err.Error())
	}

	curProcess, err = process.NewProcess(int32(os.Getpid()))
	if err != nil {
		bin.PrintErrAndExit(err.Error())
	}

	curCpuPercent, err = curProcess.CPUPercent()
	if err != nil {
		bin.PrintErrAndExit(err.Error())
	}

	otherCpuPercent := (100.0 - (totalCpuPercent[0] - curCpuPercent)) / 100.0
	go func() {
		t := time.NewTicker(3 * time.Second)
		for {
			select {
			case <-t.C:
				totalCpuPercent, err = cpu.Percent(time.Second, false)
				if err != nil {
					bin.PrintErrAndExit(err.Error())
				}

				curCpuPercent, err = curProcess.CPUPercent()
				if err != nil {
					bin.PrintErrAndExit(err.Error())
				}

				otherCpuPercent = (100.0 - (totalCpuPercent[0] - curCpuPercent)) / 100.0
			}
		}
	}()
	for i := 0; i < cpuCount; i++ {
		go func() {
			busy := int64(0)
			idle := int64(0)
			all := int64(10000000)
			dx := 0.0
			ds := time.Duration(0)
			for i := 0; ; i = (i + 1) % 1000 {
				startTime := time.Now().UnixNano()
				if i == 0 {
					dx = (float64(cpuPercent) - totalCpuPercent[0]) / otherCpuPercent
					busy = busy + int64(dx*100000)
					if busy < 0 {
						busy = 0
					}
					idle = all - busy
					if idle < 0 {
						idle = 0
					}
					ds, _ = time.ParseDuration(strconv.FormatInt(idle, 10) + "ns")
				}
				for time.Now().UnixNano()-startTime < busy {
				}
				time.Sleep(ds)
				runtime.Gosched()
			}
		}()
	}
	select {}
}

var burnCpuBin = "chaos_burncpu"

var channel = exec.NewLocalChannel()

var stopBurnCpuFunc = stopBurnCpu

var runBurnCpuFunc = runBurnCpu

var bindBurnCpuFunc = bindBurnCpuByTaskset

var checkBurnCpuFunc = checkBurnCpu

// startBurnCpu by invoke burnCpuBin with --nohup flag
func startBurnCpu() {
	ctx := context.Background()
	if cpuList != "" {
		cpuCount = 1
		cores := strings.Split(cpuList, ",")
		for _, core := range cores {
			pid := runBurnCpuFunc(ctx, cpuCount, cpuPercent, true, core)
			bindBurnCpuFunc(ctx, core, pid)
		}
	} else {
		runBurnCpuFunc(ctx, cpuCount, cpuPercent, false, "")
	}
	checkBurnCpuFunc(ctx)
}

// runBurnCpu
func runBurnCpu(ctx context.Context, cpuCount int, cpuPercent int, pidNeeded bool, processor string) int {
	args := fmt.Sprintf(`%s --nohup --cpu-count %d --cpu-percent %d`,
		path.Join(util.GetProgramPath(), burnCpuBin), cpuCount, cpuPercent)

	if pidNeeded {
		args = fmt.Sprintf("%s --cpu-processor %s", args, processor)
	}

	args = fmt.Sprintf(`%s > /dev/null 2>&1 &`, args)
	response := channel.Run(ctx, "nohup", args)
	if !response.Success {
		stopBurnCpuFunc()
		bin.PrintErrAndExit(response.Err)
	}

	if pidNeeded {
		// parse pid
		newCtx := context.WithValue(context.Background(), exec.ProcessKey, fmt.Sprintf("cpu-processor %s", processor))
		pids, err := exec.GetPidsByProcessName(burnCpuBin, newCtx)
		if err != nil {
			stopBurnCpuFunc()
			bin.PrintErrAndExit(fmt.Sprintf("bind cpu core failed, cannot get the burning program pid, %v", err))
		}
		if len(pids) > 0 {
			// return the first one
			pid, err := strconv.Atoi(pids[0])
			if err != nil {
				stopBurnCpuFunc()
				bin.PrintErrAndExit(fmt.Sprintf("bind cpu core failed, get pid failed, pids: %v, err: %v", pids, err))
			}
			return pid
		}
	}
	return -1
}

// bindBurnCpu by taskset command
func bindBurnCpuByTaskset(ctx context.Context, core string, pid int) {
	response := channel.Run(ctx, "taskset", fmt.Sprintf("-a -cp %s %d", core, pid))
	if !response.Success {
		stopBurnCpuFunc()
		bin.PrintErrAndExit(response.Err)
	}
}

// checkBurnCpu
func checkBurnCpu(ctx context.Context) {
	time.Sleep(time.Second)
	// query process
	ctx = context.WithValue(ctx, exec.ProcessKey, "nohup")
	pids, _ := exec.GetPidsByProcessName(burnCpuBin, ctx)
	if pids == nil || len(pids) == 0 {
		bin.PrintErrAndExit(fmt.Sprintf("%s pid not found", burnCpuBin))
	}
}

// stopBurnCpu
func stopBurnCpu() (success bool, errs string) {
	// add grep nohup
	ctx := context.WithValue(context.Background(), exec.ProcessKey, "nohup")
	pids, _ := exec.GetPidsByProcessName(burnCpuBin, ctx)
	if pids == nil || len(pids) == 0 {
		return true, errs
	}
	response := channel.Run(ctx, "kill", fmt.Sprintf(`-9 %s`, strings.Join(pids, " ")))
	if !response.Success {
		return false, response.Err
	}
	return true, errs
}
