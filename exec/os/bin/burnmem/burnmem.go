package main

import (
	"context"
	"flag"
	"fmt"
	"os"
	"path"
	"strconv"
	"strings"
	"time"

	"github.com/chaosblade-io/chaosblade/exec"
	"github.com/chaosblade-io/chaosblade/exec/os/bin"
	"github.com/chaosblade-io/chaosblade/transport"
	"github.com/chaosblade-io/chaosblade/util"
	"github.com/shirou/gopsutil/mem"
)

var (
	burnMemStart, burnMemStop, burnMemNohup bool
	memPercent                              int
)

func main() {
	flag.BoolVar(&burnMemStart, "start", false, "start burn memory")
	flag.BoolVar(&burnMemStop, "stop", false, "stop burn memory")
	flag.BoolVar(&burnMemNohup, "nohup", false, "nohup to run burn memory")
	flag.IntVar(&memPercent, "mem-percent", 0, "percent of burn memory")
	flag.Parse()

	if burnMemStart {
		startBurnMem()
	} else if burnMemStop {
		if success, errs := stopBurnMem(); !success {
			bin.PrintErrAndExit(errs)
		}
	} else if burnMemNohup {
		burnMem()
	} else {
		bin.PrintAndExitWithErrPrefix("less --start of --stop flag")
	}

}

var dirName = "burnmem_tmpfs"

var fileName = "file"

var fileCount = 0

func getMem(filePath string) int64 {
	sum := int64(0)
	if 0 == fileCount {
		return sum
	}
	fileInfo, err := os.Stat(filePath + strconv.Itoa(fileCount-1))
	if err != nil {
		bin.PrintErrAndExit(err.Error())
	}
	sum += fileInfo.Size()/1024/1024 + int64(fileCount-1)*128
	return sum
}

func burnMem() {

	filePath := path.Join(path.Join(util.GetProgramPath(), dirName), fileName)

	avPercent := 100 - memPercent

	go func() {
		t := time.NewTicker(3 * time.Second)
		for {
			select {
			case <-t.C:
				virtualMemory, err := mem.VirtualMemory()
				if err != nil {
					bin.PrintErrAndExit(err.Error())
				}

				// diff := float64(virtualMemory.Available)/float64(virtualMemory.Total) - float64(avPercent)/100.0

				// if diff > -0.001 && diff < 0.001 {
				// 	continue
				// }

				memSum := getMem(filePath)

				needMem := (int64(virtualMemory.Available)-int64(virtualMemory.Total)*int64(avPercent)/100)/1024/1024 + memSum

				if needMem <= 0 {
					for i := 0; i < fileCount; i++ {
						os.Remove(filePath + strconv.Itoa(i))
					}
					fileCount = 0
				} else {
					if memSum%128 != 0 {
						os.Remove(filePath + strconv.Itoa(fileCount-1))
						memSum -= memSum % 128
						fileCount--
					}
					if needMem/128 > memSum/128 {
						for i := memSum / 128; i < needMem/128; i++ {
							nFilePath := filePath + strconv.FormatInt(i, 10)
							response := channel.Run(context.Background(), "dd", fmt.Sprintf("if=/dev/zero of=%s bs=1M count=%d", nFilePath, 128))
							if !response.Success {
								bin.PrintErrAndExit(response.Error())
							}
						}
					} else {
						for i := needMem / 128; i < memSum/128; i++ {
							nFilePath := filePath + strconv.FormatInt(i, 10)
							os.RemoveAll(nFilePath)
						}
					}
					fileCount = int(needMem / 128)
					if needMem%128 != 0 {
						nFilePath := filePath + strconv.Itoa(fileCount)
						response := channel.Run(context.Background(), "dd", fmt.Sprintf("if=/dev/zero of=%s bs=1M count=%d", nFilePath, needMem%128))
						if !response.Success {
							bin.PrintErrAndExit(response.Error())
						}
						fileCount++
					}
				}
			}
		}
	}()
	select {}
}

var burnMemBin = "chaos_burnmem"

var channel = exec.NewLocalChannel()

var stopBurnMemFunc = stopBurnMem

var runBurnMemFunc = runBurnMem

func startBurnMem() {
	ctx := context.Background()

	flPath := path.Join(util.GetProgramPath(), dirName)

	if _, err := os.Stat(flPath); err != nil {
		err = os.Mkdir(flPath, os.ModePerm)
		if err != nil {
			bin.PrintErrAndExit(err.Error())
		}
	}

	response := channel.Run(ctx, "mount", fmt.Sprintf("-t tmpfs tmpfs %s -o size=", flPath)+"100%")

	if !response.Success {
		bin.PrintErrAndExit(response.Error())
	}

	runBurnMemFunc(ctx, memPercent)
}

func runBurnMem(ctx context.Context, memPercent int) int {
	args := fmt.Sprintf(`%s --nohup --mem-percent %d`,
		path.Join(util.GetProgramPath(), burnMemBin), memPercent)

	args = fmt.Sprintf(`%s > /dev/null 2>&1 &`, args)
	response := channel.Run(ctx, "nohup", args)
	if !response.Success {
		stopBurnMemFunc()
		bin.PrintErrAndExit(response.Err)
	}
	return -1
}

func stopBurnMem() (success bool, errs string) {
	ctx := context.WithValue(context.Background(), exec.ProcessKey, "nohup")
	pids, _ := exec.GetPidsByProcessName(burnMemBin, ctx)
	var response *transport.Response
	if pids != nil && len(pids) != 0 {
		response = channel.Run(ctx, "kill", fmt.Sprintf(`-9 %s`, strings.Join(pids, " ")))
		if !response.Success {
			return false, response.Err
		}
	}

	dirPath := path.Join(util.GetProgramPath(), dirName)

	if _, err := os.Stat(dirPath); err == nil {
		response = channel.Run(ctx, "umount", dirPath)

		if !response.Success {
			bin.PrintErrAndExit(response.Error())
		}

		err = os.RemoveAll(dirPath)
		if err != nil {
			bin.PrintErrAndExit(err.Error())
		}
	}

	return true, errs
}
