package jvm

import (
	"context"
	"fmt"
	"github.com/chaosblade-io/chaosblade/exec"
	"github.com/chaosblade-io/chaosblade/transport"
	"github.com/chaosblade-io/chaosblade/util"
	"os"
	"path"
	"strings"
	"time"
)

// attach sandbox to java process
var channel = exec.NewLocalChannel()

const DefaultNamespace = "default"

func Attach(processName string, port string, javaHome string) *transport.Response {
	// get process pid
	ctx := context.Background()
	ctx = context.WithValue(ctx, exec.ProcessKey, "java")
	pids, err := exec.GetPidsByProcessName(processName, ctx)
	if err != nil {
		return transport.ReturnFail(transport.Code[transport.GetProcessError], err.Error())
	}
	if pids == nil || len(pids) == 0 {
		return transport.ReturnFail(transport.Code[transport.GetProcessError], "process not found")
	}
	if len(pids) != 1 {
		return transport.ReturnFail(transport.Code[transport.GetProcessError], "too many process")
	}
	pid := pids[0]
	// refresh
	response := attach(pid, port, ctx, javaHome)
	if !response.Success {
		return response
	}
	time.Sleep(5 * time.Second)
	// active
	response = active(port)
	if !response.Success {
		return response
	}
	// check
	return check(port)
}

// curl -s http://localhost:$2/sandbox/default/module/http/chaosblade/status 2>&1
func check(port string) *transport.Response {
	url := getSandboxUrl(port, "chaosblade/status", "")
	result, err := util.Curl(url)
	if err != nil {
		return transport.ReturnFail(transport.Code[transport.SandboxInvokeError], err.Error())
	}
	return transport.ReturnSuccess(result)
}

// active chaosblade bin/sandbox.sh -p $pid -P $2 -a chaosblade 2>&1
func active(port string) *transport.Response {
	url := getSandboxUrl(port, "sandbox-module-mgr/active", "&ids=chaosblade")
	_, err := util.Curl(url)
	if err != nil {
		return transport.ReturnFail(transport.Code[transport.SandboxInvokeError], err.Error())
	}
	return transport.ReturnSuccess("success")
}

// attach java agent to application process
func attach(pid, port string, ctx context.Context, javaHome string) *transport.Response {

	if javaHome == "" {
		javaHome = os.Getenv("JAVA_HOME")
	}
	if javaHome == "" {
		return transport.ReturnFail(transport.Code[transport.EnvironmentError], "JAVA_HOME env not found")
	}
	toolsJar := path.Join(util.GetBinPath(), "tools.jar")
	originalJar := path.Join(javaHome, "lib/tools.jar")
	if util.IsExist(originalJar) {
		toolsJar = originalJar
	}
	// create sandbox token
	response := channel.Run(ctx, "date", "| head | cksum | sed 's/ //g'")
	if !response.Success {
		return response
	}
	token := strings.TrimSpace(response.Result.(string))
	jvmOpts := fmt.Sprintf("-Xms128M -Xmx128M -Xnoclassgc -ea -Xbootclasspath/a:%s", toolsJar)
	sandboxHome := path.Join(util.GetLibHome(), "sandbox")
	sandboxLibPath := path.Join(sandboxHome, "lib")
	sandboxAttachArgs := fmt.Sprintf("home=%s;token=%s;server.ip=%s;server.port=%s;namespace=%s",
		sandboxHome, token, "127.0.0.1", port, DefaultNamespace)
	javaArgs := fmt.Sprintf(`%s -jar %s/sandbox-core.jar %s "%s/sandbox-agent.jar" "%s"`,
		jvmOpts, sandboxLibPath, pid, sandboxLibPath, sandboxAttachArgs)
	response = channel.Run(ctx, path.Join(javaHome, "bin/java"), javaArgs)
	if !response.Success {
		return response
	}
	tokenFile := path.Join(util.GetUserHome(), ".sandbox.token")
	response = channel.Run(ctx, "grep", fmt.Sprintf(`%s %s | grep %s | tail -1 | awk -F ";" '{print $3";"$4}'`,
		token, tokenFile, DefaultNamespace))
	if !response.Success {
		return transport.ReturnFail(transport.Code[transport.SandboxInvokeError],
			fmt.Sprintf("attach JVM %s failed, loss response; %s", pid, response.Err))
	}
	return response
}

func Detach(port string) *transport.Response {
	return shutdown(port)
}

// sudo -u $user -H bash bin/sandbox.sh -p $pid -S 2>&1
func shutdown(port string) *transport.Response {
	url := getSandboxUrl(port, "sandbox-control/shutdown", "")
	_, err := util.Curl(url)
	if err != nil {
		return transport.ReturnFail(transport.Code[transport.SandboxInvokeError], err.Error())
	}
	return transport.ReturnSuccess("success")
}

func getSandboxUrl(port, uri, param string) string {
	// "sandbox-module-mgr/reset"
	return fmt.Sprintf("http://127.0.0.1:%s/sandbox/%s/module/http/%s?1=1%s",
		port, DefaultNamespace, uri, param)
}
