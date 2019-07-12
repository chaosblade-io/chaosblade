package os

import (
	"context"
	"fmt"
	"path"
	"runtime"
	"strconv"

	"github.com/chaosblade-io/chaosblade/exec"
	"github.com/chaosblade-io/chaosblade/transport"
	"strings"
)

type CpuCommandModelSpec struct {
}

func (*CpuCommandModelSpec) Name() string {
	return "cpu"
}

func (*CpuCommandModelSpec) ShortDesc() string {
	return "Cpu experiment"
}

func (*CpuCommandModelSpec) LongDesc() string {
	return "Cpu experiment, for example full load"
}

func (*CpuCommandModelSpec) Example() string {
	return "cpu fullload"
}

func (*CpuCommandModelSpec) Actions() []exec.ExpActionCommandSpec {
	return []exec.ExpActionCommandSpec{
		&fullLoadActionCommand{},
	}
}

func (cms *CpuCommandModelSpec) Flags() []exec.ExpFlagSpec {
	return []exec.ExpFlagSpec{
		&exec.ExpFlag{
			Name:     "cpu-count",
			Desc:     "Cpu count",
			Required: false,
		},
		&exec.ExpFlag{
			Name:     "cpu-list",
			Desc:     "CPUs in which to allow burning (0-3 or 1,3)",
			Required: false,
		},
	}
}

func (*CpuCommandModelSpec) PreExecutor() exec.PreExecutor {
	return &cpuPreExecutor{}
}

type cpuPreExecutor struct {
}

func (*cpuPreExecutor) PreExec(cmdName, parentCmdName string, flags map[string]string) func(ctx context.Context) (exec.Channel, context.Context, error) {
	return nil
}

type fullLoadActionCommand struct {
}

func (*fullLoadActionCommand) Name() string {
	return "fullload"
}

func (*fullLoadActionCommand) Aliases() []string {
	return []string{"fl"}
}

func (*fullLoadActionCommand) ShortDesc() string {
	return "cpu fullload"
}

func (*fullLoadActionCommand) LongDesc() string {
	return "cpu fullload"
}

func (*fullLoadActionCommand) Matchers() []exec.ExpFlagSpec {
	return []exec.ExpFlagSpec{}
}

func (*fullLoadActionCommand) Flags() []exec.ExpFlagSpec {
	return []exec.ExpFlagSpec{}
}

func (*fullLoadActionCommand) Executor(channel exec.Channel) exec.Executor {
	return &cpuExecutor{
		channel: channel,
	}
}

type cpuExecutor struct {
	channel exec.Channel
}

func (ce *cpuExecutor) Name() string {
	return "cpu"
}

func (ce *cpuExecutor) SetChannel(channel exec.Channel) {
	ce.channel = channel
}

func (ce *cpuExecutor) Exec(uid string, ctx context.Context, model *exec.ExpModel) *transport.Response {
	if ce.channel == nil {
		return transport.ReturnFail(transport.Code[transport.ServerError], "channel is nil")
	}
	if _, ok := exec.IsDestroy(ctx); ok {
		return ce.stop(ctx)
	}

	var cpuCount int
	var cpuList string

	cpuListStr := model.ActionFlags["cpu-list"]
	if cpuListStr != "" {
		if !exec.IsCommandAvailable("taskset") {
			return transport.ReturnFail(transport.Code[transport.EnvironmentError],
				"taskset command not exist")
		}
		cores, err := parseCpuList(cpuListStr)
		if err != nil {
			return transport.ReturnFail(transport.Code[transport.IllegalParameters],
				fmt.Sprintf("parse cpu-list flag err, %v", err))
		}
		cpuList = strings.Join(cores, ",")
	} else {
		// if cpu-list value is not empty, then the cpu-count flag is invalid
		var err error
		cpuCountStr := model.ActionFlags["cpu-count"]
		if cpuCountStr != "" {
			cpuCount, err = strconv.Atoi(cpuCountStr)
			if err != nil {
				return transport.ReturnFail(transport.Code[transport.IllegalParameters],
					"--cpu-count value must be a positive integer")
			}
		}
		if cpuCount <= 0 || int(cpuCount) > runtime.NumCPU() {
			cpuCount = runtime.NumCPU()
		}
	}
	return ce.start(ctx, cpuList, cpuCount)
}

const burnCpuBin = "chaos_burncpu"

// start burn cpu
func (ce *cpuExecutor) start(ctx context.Context, cpuList string, cpuCount int) *transport.Response {

	args := fmt.Sprintf("--start --cpu-count %d", cpuCount)
	if cpuList != "" {
		args = fmt.Sprintf("%s --cpu-list %s", args, cpuList)
	}
	return ce.channel.Run(ctx, path.Join(ce.channel.GetScriptPath(), burnCpuBin), args)
}

// stop burn cpu
func (ce *cpuExecutor) stop(ctx context.Context) *transport.Response {
	return ce.channel.Run(ctx, path.Join(ce.channel.GetScriptPath(), burnCpuBin), "--stop")
}

// parseCpuList returns the cpu core count. 0,2-3
func parseCpuList(cpuListValue string) ([]string, error) {
	cores := make([]string, 0)
	commaParts := strings.Split(cpuListValue, ",")
	for _, part := range commaParts {
		value := strings.TrimSpace(part)
		if value == "" {
			continue
		}
		if !strings.Contains(value, "-") {
			_, err := strconv.Atoi(value)
			if err != nil {
				return cores, fmt.Errorf("%s value is illegal, %v", value, err)
			}
			cores = append(cores, value)
			continue
		}
		coreRange := strings.Split(value, "-")
		if len(coreRange) != 2 {
			return cores, fmt.Errorf("%s value is illegal", value)
		}
		startIndex, err := strconv.Atoi(strings.TrimSpace(coreRange[0]))
		if err != nil || startIndex < 0 {
			return cores, fmt.Errorf("start in %s value is illegal", value)
		}
		endIndex, err := strconv.Atoi(strings.TrimSpace(coreRange[1]))
		if err != nil || endIndex < 0 {
			return cores, fmt.Errorf("end in %s value is illegal", value)
		}
		for i := startIndex; i <= endIndex; i++ {
			cores = append(cores, strconv.Itoa(i))
		}
	}
	return cores, nil
}
