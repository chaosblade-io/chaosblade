package os

import (
	"github.com/chaosblade-io/chaosblade/exec"
	"github.com/chaosblade-io/chaosblade/transport"
	"context"
	"fmt"
	"github.com/chaosblade-io/chaosblade/util"
	"strings"
)

type ScriptCommandModelSpec struct {
}

func (*ScriptCommandModelSpec) Name() string {
	return "script"
}

func (*ScriptCommandModelSpec) ShortDesc() string {
	return "Script chaos experiment"
}

func (*ScriptCommandModelSpec) LongDesc() string {
	return "Script chaos experiment"
}

func (*ScriptCommandModelSpec) Example() string {
	return `blade create script delay --time 2000 --file xxx.sh --function-name start

blade create script exit --file xxx.sh --function-name offline --exit-message "error" --exit-code 2`
}

func (*ScriptCommandModelSpec) Actions() []exec.ExpActionCommandSpec {
	return []exec.ExpActionCommandSpec{
		&ScriptDelayActionCommand{},
		&ScriptExitActionCommand{},
	}
}

func (*ScriptCommandModelSpec) Flags() []exec.ExpFlagSpec {
	return []exec.ExpFlagSpec{
		&exec.ExpFlag{
			Name:     "file",
			Desc:     "Script file full path",
			Required: true,
		},
		&exec.ExpFlag{
			Name:     "function-name",
			Desc:     "function name in shell",
			Required: true,
		},
	}
}

func (*ScriptCommandModelSpec) PreExecutor() exec.PreExecutor {
	return nil
}

const bakFileSuffix = "_chaosblade.bak"

// backScript
func backScript(channel exec.Channel, scriptFile string) *transport.Response {
	var bakFile = getBackFile(scriptFile)
	if util.IsExist(bakFile) {
		return transport.ReturnFail(transport.Code[transport.StatusError],
			fmt.Sprintf("%s backup file exists, may be annother experiment is running", bakFile))
	}
	return channel.Run(context.TODO(), "cat", fmt.Sprintf("%s > %s", scriptFile, bakFile))
}

func recoverScript(channel exec.Channel, scriptFile string) *transport.Response {
	var bakFile = getBackFile(scriptFile)
	if !util.IsExist(bakFile) {
		return transport.ReturnFail(transport.Code[transport.FileNotFound],
			fmt.Sprintf("%s backup file not exists", bakFile))
	}
	response := channel.Run(context.TODO(), "cat", fmt.Sprintf("%s > %s", bakFile, scriptFile))
	if !response.Success {
		return response
	}
	return channel.Run(context.TODO(), "rm", fmt.Sprintf("-rf %s", bakFile))
}

func getBackFile(scriptFile string) string {
	return scriptFile + bakFileSuffix
}

// awk '/offline\s?\(\)\s*\{/{print NR}' tt.sh
// sed -i '416 a sleep 100' tt.sh
func insertContentToScriptBy(channel exec.Channel, functionName string, newContent, scriptFile string) *transport.Response {
	// search line number by function name
	response := channel.Run(context.TODO(), "awk", fmt.Sprintf(`'/%s *\(\) *\{/{print NR}' %s`, functionName, scriptFile))
	if !response.Success {
		return response
	}
	result := strings.TrimSpace(response.Result.(string))
	lineNums := strings.Split(result, "\n")
	if len(lineNums) > 1 {
		return transport.ReturnFail(transport.Code[transport.IllegalParameters],
			fmt.Sprintf("get too many lines by the %s function name", functionName))
	}
	if len(lineNums) == 0 || strings.TrimSpace(lineNums[0]) == "" {
		return transport.ReturnFail(transport.Code[transport.IllegalParameters],
			fmt.Sprintf("cannot find the %s function name", functionName))
	}
	lineNum := lineNums[0]
	// insert content to the line below
	return channel.Run(context.TODO(), "sed", fmt.Sprintf(`-i '%s a %s' %s`, lineNum, newContent, scriptFile))
}
