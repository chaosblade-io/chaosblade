package cplus

import (
	"github.com/chaosblade-io/chaosblade/transport"
	"github.com/chaosblade-io/chaosblade/util"
	"encoding/json"
	"fmt"
	"github.com/chaosblade-io/chaosblade/exec"
	"context"
	"path"
	"time"
	"os"
)

const ApplicationName = "chaosblade-exec-cplus.jar"
const RemoveAction = "remove"

var cplusJarPath = path.Join(util.GetLibHome(), "cplus", ApplicationName)
var scriptDefaultPath = path.Join(util.GetLibHome(), "cplus", "script")

// 启动 spring boot application，需要校验程序是否已启动
func Prepare(port, scriptLocation string, waitTime int, javaHome string) *transport.Response {
	if scriptLocation == "" {
		scriptLocation = scriptDefaultPath + "/"
	}
	response := preCheck(port, scriptLocation)
	if !response.Success {
		return response
	}
	javaBin, err := getJavaBin(javaHome)
	if err != nil {
		return transport.ReturnFail(transport.Code[transport.FileNotFound], err.Error())
	}
	response = startProxy(port, scriptLocation, javaBin)
	if !response.Success {
		return response
	}
	// wait seconds
	time.Sleep(time.Duration(waitTime) * time.Second)
	return postCheck(port)
}

// getJavaBin returns the java bin path
func getJavaBin(javaHome string) (string, error) {
	if javaHome == "" {
		// check java bin
		response := exec.NewLocalChannel().Run(context.Background(), "java", "-version")
		if response.Success {
			return "java", nil
		}
		// get java home
		javaHome = os.Getenv("JAVA_HOME")
		if javaHome == "" {
			return "", fmt.Errorf("JAVA_HOME not found")
		}
	}
	javaBin := path.Join(javaHome, "bin", "java")
	response := exec.NewLocalChannel().Run(context.Background(), javaBin, "-version")
	if !response.Success {
		return "", fmt.Errorf(response.Err)
	}
	return javaBin, nil
}

func preCheck(port, scriptLocation string) *transport.Response {
	// check spring boot application
	if processExists(port) {
		return transport.ReturnFail(transport.Code[transport.DuplicateError], "the server proxy has been started")
	}
	// check chaosblade-exec-cplus.jar file exists or not
	if !util.IsExist(cplusJarPath) {
		return transport.ReturnFail(transport.Code[transport.FileNotFound],
			fmt.Sprintf("the %s proxy jar file not found in %s dir", ApplicationName, util.GetLibHome()))
	}
	// check script file
	if !util.IsExist(scriptLocation) {
		return transport.ReturnFail(transport.Code[transport.FileNotFound],
			fmt.Sprintf("the %s script file dir not found", scriptLocation))
	}
	// check the port has been used or not
	portInUse := util.CheckPortInUse(port)
	if portInUse {
		return transport.ReturnFail(transport.Code[transport.IllegalParameters],
			fmt.Sprintf("the %s port is in use", port))
	}
	return transport.ReturnSuccess("success")
}

func processExists(port string) bool {
	ctx := context.WithValue(context.Background(), exec.ProcessKey, port)
	pids, _ := exec.GetPidsByProcessName(ApplicationName, ctx)
	if pids != nil && len(pids) > 0 {
		return true
	}
	return false
}

// startProxy invokes `nohup java -jar chaosblade-exec-cplus-1.0-SNAPSHOT1.jar --server.port=8703 --script.location=xxx &`
func startProxy(port, scriptLocation, javaBin string) *transport.Response {
	args := fmt.Sprintf("%s -jar %s --server.port=%s --script.location=%s >> %s 2>&1 &",
		javaBin,
		cplusJarPath,
		port, scriptLocation,
		util.GetNohupOutput(util.Blade, util.BladeLog))
	return exec.NewLocalChannel().Run(context.Background(), "nohup", args)
}

func postCheck(port string) *transport.Response {
	result, err, _ := util.Curl(getProxyServiceUrl(port, "status"))
	if err != nil {
		return transport.ReturnFail(transport.Code[transport.CplusProxyCmdError], err.Error())
	}
	var resp transport.Response
	json.Unmarshal([]byte(result), &resp)
	return &resp
}

// 停止 spring boot application
func Revoke(port string) *transport.Response {
	// check process
	if !processExists(port) {
		return transport.ReturnSuccess("process not exists")
	}

	// Get http://127.0.0.1:xxx/remove: EOF, doesn't to check the result
	util.Curl(getProxyServiceUrl(port, RemoveAction))

	time.Sleep(2 * time.Second)
	// revoke failed if the check operation returns success
	response := postCheck(port)
	if response.Success {
		return transport.ReturnFail(transport.Code[transport.CplusProxyCmdError], "the process exists")
	}
	return transport.ReturnSuccess("success")
}

func getProxyServiceUrl(port, action string) string {
	return fmt.Sprintf("http://127.0.0.1:%s/%s",
		port, action)
}
