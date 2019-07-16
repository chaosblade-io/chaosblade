package exec

import (
	"context"
	"time"
	"strings"
	"fmt"
	"github.com/chaosblade-io/chaosblade/transport"
	"github.com/chaosblade-io/chaosblade/util"
	"os/exec"
)

var channel = &LocalChannel{}

type LocalChannel struct {
}

func NewLocalChannel() Channel {
	return channel
}

func (client *LocalChannel) Run(ctx context.Context, script, args string) *transport.Response {
	return execScript(ctx, script, args)
}

func (client *LocalChannel) GetScriptPath() string {
	return util.GetBinPath()
}

func execScript(ctx context.Context, script, args string) *transport.Response {
	newCtx, cancel := context.WithTimeout(ctx, 60*time.Second)
	defer cancel()
	if ctx == context.Background() {
		ctx = newCtx
	}
	cmd := exec.CommandContext(ctx, "/bin/sh", "-c", script+" "+args)
	output, err := cmd.CombinedOutput()
	if err != nil {
		errMsg := fmt.Sprintf(string(output) + " " + err.Error())
		return transport.ReturnFail(transport.Code[transport.ExecCommandError], errMsg)
	}
	result := string(output)
	return transport.ReturnSuccess(result)
}

// GetPidsByProcessCmdName
func GetPidsByProcessCmdName(processName string, ctx context.Context) ([]string, error) {
	response := channel.Run(ctx, "pgrep",
		fmt.Sprintf(`-l %s | grep -v -w blade | grep -v -w chaos_stopprocess | grep -v -w chaos_killprocess | awk '{print $1}' | tr '\n' ' '`, processName))
	if !response.Success {
		return nil, fmt.Errorf(response.Err)
	}
	pidString := response.Result.(string)
	return strings.Fields(strings.TrimSpace(pidString)), nil
}

// grep ${key}
const ProcessKey = "process"

// GetPidsByProcessName
func GetPidsByProcessName(processName string, ctx context.Context) ([]string, error) {
	psArgs := GetPsArgs(ctx)
	otherProcess := ctx.Value(ProcessKey)
	otherGrepInfo := ""
	if otherProcess != nil {
		processString := otherProcess.(string)
		if processString != "" {
			otherGrepInfo = fmt.Sprintf(`| grep "%s"`, processString)
		}
	}
	response := channel.Run(ctx, "ps",
		fmt.Sprintf(`%s | grep %s %s | grep -v -w grep | grep -v -w blade | grep -v -w chaos_stopprocess | grep -v -w chaos_killprocess | awk '{print $2}' | tr '\n' ' '`,
			psArgs, processName, otherGrepInfo))
	if !response.Success {
		return nil, fmt.Errorf(response.Err)
	}
	pidString := response.Result.(string)
	return strings.Fields(strings.TrimSpace(pidString)), nil
}

// GetPsArgs
func GetPsArgs(ctx context.Context) string {
	var osVer = ""
	if util.IsExist("/etc/os-release") {
		response := channel.Run(ctx, "awk", "-F '=' '{if ($1 == \"ID\") {print $2;exit 0}}' /etc/os-release")
		if response.Success {
			osVer = response.Result.(string)
		}
	}
	var psArgs = "-ef"
	if strings.TrimSpace(osVer) == "alpine" {
		psArgs = "-o user,pid,ppid,args"
	}
	return psArgs
}

// IsCommandAvailable return true if the command exists
func IsCommandAvailable(commandName string) bool {
	response := execScript(context.TODO(), "command", fmt.Sprintf("-v %s", commandName))
	return response.Success
}
