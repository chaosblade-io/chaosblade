/*
 * Copyright 2025 The ChaosBlade Authors
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 */

package python

import (
	"context"
	"fmt"
	"os"
	"path"
	"strings"

	"github.com/chaosblade-io/chaosblade-spec-go/channel"
	"github.com/chaosblade-io/chaosblade-spec-go/log"
	"github.com/chaosblade-io/chaosblade-spec-go/spec"
	"github.com/chaosblade-io/chaosblade-spec-go/util"
)

const (
	PreparePythonType = "python"
	ApplicationName   = "chaosblade-exec-python"
)

var (
	pythonLibPath = path.Join(util.GetLibHome(), "python")
)

// Prepare installs the python agent hook into the target script directory.
// The python agent runs in-process, so this only generates sitecustomize.py
// and records the preparation; no separate process is started.
func Prepare(ctx context.Context, port, pythonPath, targetScript string) *spec.Response {
	response := preCheck(ctx, port, pythonPath, targetScript)
	if !response.Success {
		return response
	}

	targetDir := path.Dir(targetScript)
	if err := os.MkdirAll(targetDir, 0o755); err != nil {
		log.Errorf(ctx, "create target script directory %s failed, %v", targetDir, err)
		return spec.ResponseFailWithFlags(spec.ChaosbladeFileNotFound, targetDir)
	}

	response = generateSiteCustomize(ctx, port, pythonPath, targetDir)
	if !response.Success {
		return response
	}

	response = generatePythonPathEnv(targetDir)
	if !response.Success {
		return response
	}

	return Status(ctx, port)
}

func preCheck(ctx context.Context, port, pythonPath, targetScript string) *spec.Response {
	if pythonPath == "" {
		return spec.ResponseFailWithFlags(spec.ParameterLess, "python-path")
	}
	if targetScript == "" {
		return spec.ResponseFailWithFlags(spec.ParameterLess, "target-script")
	}
	if !util.IsExist(pythonPath) {
		log.Errorf(ctx, "%s", spec.ChaosbladeFileNotFound.Sprintf(pythonPath))
		return spec.ResponseFailWithFlags(spec.ChaosbladeFileNotFound, pythonPath)
	}
	portInUse := util.CheckPortInUse(port)
	if portInUse {
		log.Errorf(ctx, "%s", spec.ParameterInvalid.Sprintf("port", port, "the port has been used"))
		return spec.ResponseFailWithFlags(spec.ParameterInvalid, "port", port, "the port has been used")
	}
	return spec.ReturnSuccess("success")
}

func generateSiteCustomize(ctx context.Context, port, pythonPath, targetDir string) *spec.Response {
	siteCustomizePath := path.Join(targetDir, "sitecustomize.py")
	content := fmt.Sprintf(`import sys
import os

# Add chaosblade python agent library path
agent_path = "%s"
if agent_path not in sys.path:
    sys.path.insert(0, agent_path)

try:
    from chaosblade import ChaosBladeAgent
    _port = int(os.environ.get("CHAOSBLADE_PYTHON_AGENT_PORT", "%s"))
    ChaosBladeAgent(port=_port).start()
except Exception as e:
    print("[chaosblade] failed to start python agent: {}".format(e), file=sys.stderr)
`, pythonLibPath, port)

	if err := os.WriteFile(siteCustomizePath, []byte(content), 0o644); err != nil {
		log.Errorf(ctx, "write sitecustomize.py failed, %v", err)
		return spec.ResponseFailWithFlags(spec.ChaosbladeFileNotFound, siteCustomizePath)
	}

	return channel.NewLocalChannel().Run(ctx, pythonPath, fmt.Sprintf("-m py_compile %s", siteCustomizePath))
}

func generatePythonPathEnv(targetDir string) *spec.Response {
	envFile := path.Join(targetDir, "chaosblade_python_env.sh")
	value := fmt.Sprintf("export PYTHONPATH=\"%s:%s:$PYTHONPATH\"\n", targetDir, pythonLibPath)
	if err := os.WriteFile(envFile, []byte(value), 0o644); err != nil {
		return spec.ResponseFailWithFlags(spec.ChaosbladeFileNotFound, envFile)
	}
	return spec.ReturnSuccess("success")
}

// Revoke removes the python agent hook. The agent itself lives in the target
// python process and will be removed on the next process restart.
func Revoke(ctx context.Context, port string) *spec.Response {
	record, err := db.QueryRunningPreByTypeAndProcess(PreparePythonType, port, "")
	if err != nil {
		log.Errorf(ctx, "%s", spec.DatabaseError.Sprintf("query", err))
		return spec.ResponseFailWithFlags(spec.DatabaseError, "query", err)
	}
	if record == nil || record.Pid == "" {
		return spec.ReturnSuccess("no hook installed")
	}

	targetDir := path.Dir(record.Pid)
	siteCustomizePath := path.Join(targetDir, "sitecustomize.py")
	envFile := path.Join(targetDir, "chaosblade_python_env.sh")

	if util.IsExist(siteCustomizePath) {
		if err := os.Remove(siteCustomizePath); err != nil {
			log.Errorf(ctx, "remove sitecustomize.py failed, %v", err)
			return spec.ResponseFailWithFlags(spec.ChaosbladeFileNotFound, siteCustomizePath)
		}
	}
	if util.IsExist(envFile) {
		_ = os.Remove(envFile)
	}

	return spec.ReturnSuccess("success")
}

// Status checks whether the python agent HTTP endpoint is reachable.
func Status(ctx context.Context, port string) *spec.Response {
	url := getServiceUrl(port, "status")
	result, err, code := util.Curl(ctx, url)
	if err != nil {
		if strings.Contains(err.Error(), "connection refused") {
			return spec.ReturnSuccess("python agent is not started, hook installed")
		}
		log.Errorf(ctx, "%s", spec.HttpExecFailed.Sprintf(url, err))
		return spec.ResponseFailWithFlags(spec.HttpExecFailed, url, err)
	}
	if code != 200 {
		log.Errorf(ctx, "%s", spec.HttpExecFailed.Sprintf(url, result))
		return spec.ResponseFailWithFlags(spec.HttpExecFailed, url, result)
	}
	return spec.ReturnSuccess(result)
}

func getServiceUrl(port, action string) string {
	return fmt.Sprintf("http://127.0.0.1:%s/%s", port, action)
}
