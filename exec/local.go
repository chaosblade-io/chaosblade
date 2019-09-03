package exec

import (
	"context"
	"fmt"
	"github.com/chaosblade-io/chaosblade/transport"
	"github.com/chaosblade-io/chaosblade/util"
	"os/exec"
	"strings"
	"time"
	"github.com/sirupsen/logrus"
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
	logrus.Debugf("script: %s %s", script, args)
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
		fmt.Sprintf(`-l %s | grep -v -w blade | grep -v -w chaos_killprocess | grep -v -w chaos_stopprocess | awk '{print $1}' | tr '\n' ' '`, processName))
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
	psArgs := GetPsArgs()
	otherProcess := ctx.Value(ProcessKey)
	otherGrepInfo := ""
	if otherProcess != nil {
		processString := otherProcess.(string)
		if processString != "" {
			otherGrepInfo = fmt.Sprintf(`| grep "%s"`, processString)
		}
	}
	response := channel.Run(ctx, "ps",
		fmt.Sprintf(`%s | grep %s %s | grep -v -w grep | grep -v -w blade | grep -v -w chaos_killprocess | grep -v -w chaos_stopprocess | awk '{print $2}' | tr '\n' ' '`,
			psArgs, processName, otherGrepInfo))
	if !response.Success {
		return nil, fmt.Errorf(response.Err)
	}
	pidString := strings.TrimSpace(response.Result.(string))
	if pidString == "" {
		return make([]string, 0), nil
	}
	return strings.Fields(pidString), nil
}

// GetPsArgs for querying the process info
func GetPsArgs() string {
	var psArgs = "-eo user,pid,ppid,args"
	if isAlpinePlatform() {
		psArgs = "-o user,pid,ppid,args"
	}
	return psArgs
}

// isAlpinePlatform returns true if the os version is alpine.
// If the /etc/os-release file doesn't exist, the function returns false.
func isAlpinePlatform() bool {
	var osVer = ""
	if util.IsExist("/etc/os-release") {
		response := channel.Run(context.TODO(), "awk", "-F '=' '{if ($1 == \"ID\") {print $2;exit 0}}' /etc/os-release")
		if response.Success {
			osVer = response.Result.(string)
		}
	}
	return strings.TrimSpace(osVer) == "alpine"
}

// IsCommandAvailable return true if the command exists
func IsCommandAvailable(commandName string) bool {
	response := execScript(context.TODO(), "command", fmt.Sprintf("-v %s", commandName))
	return response.Success
}

//ProcessExists returns true if the pid exists, otherwise return false.
func ProcessExists(pid string) (bool, error) {
	if isAlpinePlatform() {
		response := channel.Run(context.TODO(), "ps", fmt.Sprintf("-o pid | grep %s", pid))
		if !response.Success {
			return false, fmt.Errorf(response.Err)
		}
		if strings.TrimSpace(response.Result.(string)) == "" {
			return false, nil
		}
		return true, nil
	}
	response := channel.Run(context.TODO(), "ps", fmt.Sprintf("-p %s", pid))
	return response.Success, nil
}

// GetPidUser
func GetPidUser(pid string) (string, error) {
	var response *transport.Response
	if isAlpinePlatform() {
		response = channel.Run(context.TODO(), "ps", fmt.Sprintf("-o user,pid | grep %s", pid))

	} else {
		response = channel.Run(context.TODO(), "ps", fmt.Sprintf("-o user,pid -p %s | grep %s", pid, pid))
	}
	if !response.Success {
		return "", fmt.Errorf(response.Err)
	}
	result := strings.TrimSpace(response.Result.(string))
	if result == "" {
		return "", fmt.Errorf("process user not found by pid")
	}
	return strings.Fields(result)[0], nil
}
