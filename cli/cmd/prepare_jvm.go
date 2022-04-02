/*
 * Copyright 1999-2020 Alibaba Group Holding Ltd.
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

package cmd

import (
	"context"
	"encoding/json"
	"fmt"
	"github.com/chaosblade-io/chaosblade-spec-go/log"
	"path"
	"strconv"
	"strings"
	"time"

	"github.com/chaosblade-io/chaosblade-spec-go/channel"
	"github.com/chaosblade-io/chaosblade-spec-go/spec"
	"github.com/chaosblade-io/chaosblade-spec-go/util"
	"github.com/spf13/cobra"

	"github.com/chaosblade-io/chaosblade/data"
	"github.com/chaosblade-io/chaosblade/exec/jvm"
)

type PrepareJvmCommand struct {
	baseCommand
	javaHome    string
	processName string
	// sandboxHome is jvm-sandbox home, default value is CHAOSBLADE_HOME/lib
	sandboxHome string
	port        int
	processId   string
	// Whether to attach asynchronously, default is false
	async bool
	// Used to internal asynchronous attach, no need to config
	uid string
	// Used to internal asynchronous attach, no need to config
	nohup bool
	// Actively report the attach result.
	// The installation result report is triggered only when the async value is true and the value is not empty.
	endpoint string
}

func (pc *PrepareJvmCommand) Init() {
	pc.command = &cobra.Command{
		Use:   "jvm",
		Short: "Attach a type agent to the jvm process",
		Long:  "Attach a type agent to the jvm process for java framework experiment.",
		RunE: func(cmd *cobra.Command, args []string) error {
			ctx := context.WithValue(context.Background(), spec.Uid, pc.uid)
			return pc.prepareJvm(ctx)
		},
		Example: pc.prepareExample(),
	}
	pc.command.Flags().StringVarP(&pc.javaHome, "javaHome", "j", "", "the java jdk home path")
	pc.command.Flags().StringVarP(&pc.processName, "process", "p", "", "the java application process name (required)")
	pc.command.Flags().IntVarP(&pc.port, "port", "P", 0, "the port used for agent server")
	pc.command.Flags().StringVarP(&pc.processId, "pid", "", "", "the target java process id")
	pc.command.Flags().BoolVarP(&pc.async, "async", "a", false, "whether to attach asynchronously, default is false")
	pc.command.Flags().StringVarP(&pc.uid, "uid", "u", "", "used to internal async attach, no need to config")
	pc.command.Flags().BoolVarP(&pc.nohup, "nohup", "n", false, "used to internal async attach, no need to config")
	pc.command.Flags().StringVarP(&pc.endpoint, "endpoint", "e", "", "the attach result reporting address. It takes effect only when the async value is true and the value is not empty")
	pc.sandboxHome = path.Join(util.GetLibHome(), "sandbox")
}

func (pc *PrepareJvmCommand) prepareExample() string {
	return `prepare jvm --process tomcat`
}

// prepareJvm means attaching java agent
func (pc *PrepareJvmCommand) prepareJvm(ctx context.Context) error {
	if pc.processName == "" && pc.processId == "" {
		return spec.ResponseFailWithFlags(spec.ParameterLess, "process|pid")
	}
	pid, response := jvm.CheckFlagValues(ctx, pc.processName, pc.processId)
	if !response.Success {
		return response
	}
	pc.processId = pid
	record, err := GetDS().QueryRunningPreByTypeAndProcess(PrepareJvmType, pc.processName, pc.processId)
	if err != nil {
		return spec.ResponseFailWithFlags(spec.DatabaseError, "query", err)
	}
	if !pc.nohup {
		record, err = pc.ManualPreparation(ctx, record, err)
		if err != nil {
			return err
		}
		if record == nil {
			return nil
		}
	}
	if pc.uid == "" && record != nil {
		pc.uid = record.Uid
	}
	if pc.port == 0 && record != nil {
		pc.port, _ = strconv.Atoi(record.Port)
	}
	response = pc.attachAgent(ctx)
	if record != nil && record.Pid != pc.processId {
		// update pid
		updatePreparationPid(pc.uid, pc.processId)
	}

	preErr := handlePrepareResponseWithoutExit(ctx, pc.uid, pc.command, response)
	if pc.async && pc.endpoint != "" {
		pc.reportAttachedResult(ctx, response)
	}
	if preErr == nil {
		pc.command.Println(response.Print())
		return nil
	}
	return preErr
}

func (pc *PrepareJvmCommand) reportAttachedResult(ctx context.Context, response *spec.Response) {
	log.Infof(ctx, "report response: %s to endpoint: %s", response.Print(), pc.endpoint)
	body, err := createPostBody(ctx)
	if err != nil {
		log.Warnf(ctx, "create java install post body %s failed, %v", response.Print(), err)
	} else {
		result, err, code := util.PostCurl(pc.endpoint, body, "application/json")
		if err != nil {
			log.Warnf(ctx, "report java install result %s failed, %v", response.Print(), err)
		} else if code != 200 {
			log.Warnf(ctx, "response code is %d, result %s", code, result)
		} else {
			log.Infof(ctx, "report java install result success, result %s", result)
		}
	}
}

// attachAgent
func (pc *PrepareJvmCommand) attachAgent(ctx context.Context) *spec.Response {
	response, username := jvm.Attach(ctx, strconv.Itoa(pc.port), pc.javaHome, pc.processId)
	if !response.Success && username != "" && strings.Contains(response.Err, "connection refused") {
		// if attach failed, search port from ~/.sandbox.token
		port, err := jvm.CheckPortFromSandboxToken(ctx, username)
		if err == nil {
			log.Infof(ctx, "use %s port to retry", port)
			response, username = jvm.Attach(ctx, port, pc.javaHome, pc.processId)
			if response.Success {
				// update port
				err := updatePreparationPort(pc.uid, port)
				if err != nil {
					log.Warnf(ctx, "update preparation port failed, %v", err)
				}
			}
		}
	}
	return response
}

func (pc *PrepareJvmCommand) ManualPreparation(ctx context.Context, record *data.PreparationRecord, err error) (*data.PreparationRecord, error) {
	if record == nil || record.Status != "Running" {
		var port string
		if pc.port != 0 {
			// get port from flag value user passed
			port = strconv.Itoa(pc.port)
		} else {
			// get port from local port
			port, err = getAndCacheSandboxPort()
			if err != nil {
				return nil, spec.ResponseFailWithFlags(spec.SandboxGetPortFailed, err)
			}
		}
		record, err = insertPrepareRecord(PrepareJvmType, pc.processName, port, pc.processId)
		if err != nil {
			return nil, spec.ResponseFailWithFlags(spec.DatabaseError, "insert", err)
		}
	} else {
		if pc.port != 0 && strconv.Itoa(pc.port) != record.Port {
			return nil, spec.ResponseFailWithFlags(spec.SandboxGetPortFailed,
				fmt.Sprintf("the process has been executed prepare command, if you wan't re-prepare, "+
					"please append or modify the --port %s argument in prepare command for retry", record.Port))
		}
	}

	if pc.async {
		go pc.invokeAttaching(ctx, record.Port, record.Uid)
		time.Sleep(time.Second)
		pc.command.Println(spec.ReturnSuccess(record.Uid).Print())
		// return record nil value to break flow
		return nil, nil
	}
	return record, nil
}

func (pc *PrepareJvmCommand) invokeAttaching(ctx context.Context, port string, uid string) {
	args := fmt.Sprintf("prepare jvm --uid %s --nohup", uid)
	if port != "" {
		args = fmt.Sprintf("%s --port %s", args, port)
	}
	if pc.processName != "" {
		args = fmt.Sprintf("%s -p %s", args, pc.processName)
	}
	if pc.javaHome != "" {
		args = fmt.Sprintf("%s -j %s", args, pc.javaHome)
	}
	if pc.processId != "" {
		args = fmt.Sprintf("%s --pid %s", args, pc.processId)
	}
	if pc.async {
		args = fmt.Sprintf("%s --async", args)
	}
	if pc.endpoint != "" {
		args = fmt.Sprintf("%s --endpoint %s", args, pc.endpoint)
	}
	response := channel.NewLocalChannel().Run(context.Background(), path.Join(util.GetProgramPath(), "blade"), args)
	if response.Success {
		log.Infof(ctx, "attach java agent success, uid: %s", uid)
	} else {
		log.Warnf(ctx, "attach java agent failed, err: %s, uid: %s", response.Err, uid)
	}
}

/*
{
  "data":{   #PreparestatusBean
    "createTime":"",
    "error":"",
    "pid":"",
    "port":"",
    "process":"sss",
    "running":false,
    "status":"",
    "type":"",
    "uid":"",
    "updateTime":""
  },
  "type":"JAVA_AGENT_PREPARE"
}
*/
func createPostBody(ctx context.Context) ([]byte, error) {
	preparationRecord, err := GetDS().QueryPreparationByUid(uid)
	if err != nil {
		return nil, err
	}
	bodyMap := make(map[string]interface{}, 0)

	bodyMap["data"] = preparationRecord
	bodyMap["type"] = "JAVA_AGENT_PREPARE"
	// encode
	bytes, err := json.Marshal(bodyMap)
	if err != nil {
		log.Warnf(ctx, "Marshal request body to json error. %v", err)
		return nil, err
	}
	log.Infof(ctx, "body: %s", string(bytes))
	return bytes, nil
}

// getSandboxPort by process name. If this process does not exist, an unbound port will be selected
func getAndCacheSandboxPort() (string, error) {
	port, err := util.GetUnusedPort()
	if err != nil {
		return "", err
	}
	return strconv.Itoa(port), nil
}
