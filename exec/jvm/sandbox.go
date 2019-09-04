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
	"strconv"
)

// attach sandbox to java process
var channel = exec.NewLocalChannel()

const DefaultNamespace = "default"

func Attach(port string, javaHome string, pid string) (*transport.Response, string) {
	// refresh
	response, username := attach(pid, port, context.TODO(), javaHome)
	if !response.Success {
		return response, username
	}
	time.Sleep(5 * time.Second)
	// active
	response = active(port)
	if !response.Success {
		return response, username
	}
	// check
	return check(port), username
}

// curl -s http://localhost:$2/sandbox/default/module/http/chaosblade/status 2>&1
func check(port string) *transport.Response {
	url := getSandboxUrl(port, "chaosblade/status", "")
	result, err, code := util.Curl(url)
	if code == 200 {
		return transport.ReturnSuccess(result)
	}
	if err != nil {
		return transport.ReturnFail(transport.Code[transport.SandboxInvokeError], err.Error())
	}
	return transport.ReturnFail(transport.Code[transport.SandboxInvokeError],
		fmt.Sprintf("response code is %d, result: %s", code, result))
}

// active chaosblade bin/sandbox.sh -p $pid -P $2 -a chaosblade 2>&1
func active(port string) *transport.Response {
	url := getSandboxUrl(port, "sandbox-module-mgr/active", "&ids=chaosblade")
	result, err, code := util.Curl(url)
	if err != nil {
		return transport.ReturnFail(transport.Code[transport.SandboxInvokeError], err.Error())
	}
	if code != 200 {
		return transport.ReturnFail(transport.Code[transport.SandboxInvokeError],
			fmt.Sprintf("active module response code: %d, result: %s", code, result))
	}
	return transport.ReturnSuccess("success")
}

// attach java agent to application process
func attach(pid, port string, ctx context.Context, javaHome string) (*transport.Response, string) {
	if javaHome == "" {
		javaHome = os.Getenv("JAVA_HOME")
	}
	if javaHome == "" {
		psArgs := exec.GetPsArgs()
		response := channel.Run(ctx, "ps", fmt.Sprintf(`%s | grep -w %s | grep java | grep -v grep | awk '{print $4}' | sed 's/\/bin\/java//g'`,
			psArgs, pid))
		if response.Success {
			javaHome = strings.TrimSpace(response.Result.(string))
		}
	}
	if javaHome == "" {
		return transport.ReturnFail(transport.Code[transport.EnvironmentError], "JAVA_HOME env not found"), ""
	}
	user, err := exec.GetPidUser(pid)
	if err != nil {
		return transport.ReturnFail(transport.Code[transport.GetProcessError], err.Error()), user
	}
	toolsJar := path.Join(util.GetBinPath(), "tools.jar")
	originalJar := path.Join(javaHome, "lib/tools.jar")
	if util.IsExist(originalJar) {
		toolsJar = originalJar
	}
	// create sandbox token
	response := channel.Run(ctx, "date", "| head | cksum | sed 's/ //g'")
	if !response.Success {
		return response, user
	}
	token := strings.TrimSpace(response.Result.(string))
	jvmOpts := fmt.Sprintf("-Xms128M -Xmx128M -Xnoclassgc -ea -Xbootclasspath/a:%s", toolsJar)
	sandboxHome := path.Join(util.GetLibHome(), "sandbox")
	sandboxLibPath := path.Join(sandboxHome, "lib")
	sandboxAttachArgs := fmt.Sprintf("home=%s;token=%s;server.ip=%s;server.port=%s;namespace=%s",
		sandboxHome, token, "127.0.0.1", port, DefaultNamespace)
	javaArgs := fmt.Sprintf(`%s -jar %s/sandbox-core.jar %s "%s/sandbox-agent.jar" "%s"`,
		jvmOpts, sandboxLibPath, pid, sandboxLibPath, sandboxAttachArgs)
	javaBin := path.Join(javaHome, "bin/java")
	response = channel.Run(ctx, "sudo", fmt.Sprintf("-u %s %s %s", user, javaBin, javaArgs))
	if !response.Success {
		return response, user
	}
	response = channel.Run(ctx, "grep", fmt.Sprintf(`%s %s | grep %s | tail -1 | awk -F ";" '{print $3";"$4}'`,
		token, getSandboxTokenFile(user), DefaultNamespace))
	// if attach successfully, the sandbox-agent.jar will write token to local file
	if !response.Success {
		return transport.ReturnFail(transport.Code[transport.SandboxInvokeError],
			fmt.Sprintf("attach JVM %s failed, loss response; %s", pid, response.Err)), user
	}
	return response, user
}

func Detach(port string) *transport.Response {
	return shutdown(port)
}

// CheckPortFromSandboxToken will read last line and curl the port for testing connectivity
func CheckPortFromSandboxToken(username string) (port string, err error) {
	port, err = getPortFromSandboxToken(username)
	if err != nil {
		return port, err
	}
	versionUrl := getSandboxUrl(port, "sandbox-info/version", "")
	_, err, _ = util.Curl(versionUrl)
	if err != nil {
		return "", err
	}
	return port, nil
}

func getPortFromSandboxToken(username string) (port string, err error) {
	response := channel.Run(context.TODO(), "grep",
		fmt.Sprintf(`%s %s | tail -1 | awk -F ";" '{print $4}'`,
			DefaultNamespace, getSandboxTokenFile(username)))
	if !response.Success {
		return "", fmt.Errorf(response.Err)
	}
	if response.Result == nil {
		return "", fmt.Errorf("get empty from sandbox token file")
	}
	port = strings.TrimSpace(response.Result.(string))
	if port == "" {
		return "", fmt.Errorf("read empty from sandbox token file")
	}
	_, err = strconv.Atoi(port)
	if err != nil {
		return "", fmt.Errorf("can not find port from sandbox token file, %v", err)
	}
	return port, nil
}

// sudo -u $user -H bash bin/sandbox.sh -p $pid -S 2>&1
func shutdown(port string) *transport.Response {
	url := getSandboxUrl(port, "sandbox-control/shutdown", "")
	result, err, code := util.Curl(url)
	if err != nil {
		return transport.ReturnFail(transport.Code[transport.SandboxInvokeError], err.Error())
	}
	if code != 200 {
		return transport.ReturnFail(transport.Code[transport.SandboxInvokeError],
			fmt.Sprintf("shutdown module response code: %d, result: %s", code, result))
	}
	return transport.ReturnSuccess("success")
}

func getSandboxUrl(port, uri, param string) string {
	// "sandbox-module-mgr/reset"
	return fmt.Sprintf("http://127.0.0.1:%s/sandbox/%s/module/http/%s?1=1%s",
		port, DefaultNamespace, uri, param)
}

func getSandboxTokenFile(username string) string {
	userHome := util.GetSpecifyingUserHome(username)
	return path.Join(userHome, ".sandbox.token")
}
