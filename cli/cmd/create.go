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
	"os/exec"
	"path"
	"strconv"
	"time"

	"github.com/sirupsen/logrus"
	"github.com/spf13/pflag"

	"github.com/chaosblade-io/chaosblade/data"

	"github.com/chaosblade-io/chaosblade-spec-go/channel"
	"github.com/chaosblade-io/chaosblade-spec-go/spec"
	"github.com/chaosblade-io/chaosblade-spec-go/util"
	"github.com/spf13/cobra"
)

// CreateCommand for create experiment
type CreateCommand struct {
	baseCommand
	*baseExpCommandService
	async bool // Whether to create asynchronously, default is false
	// Actively report the create result.
	// The installation result report is triggered only when the async value is true and the value is not empty.
	endpoint string
	nohup    bool //used to internal async create, no need to config
}

const UidFlag = "uid"
const AsyncFlag = "async"
const EndpointFlag = "endpoint"
const NohupFlag = "nohup"

var uid string

func (cc *CreateCommand) Init() {
	cc.command = &cobra.Command{
		Use:     "create",
		Short:   "Create a chaos engineering experiment",
		Long:    "Create a chaos engineering experiment",
		Aliases: []string{"c"},
		Example: createExample(),
	}
	flags := cc.command.PersistentFlags()
	flags.StringVar(&uid, UidFlag, "", "Set Uid for the experiment, adapt to docker and cri")
	flags.BoolVarP(&cc.async, AsyncFlag, "a", false, "whether to create asynchronously, default is false")
	flags.StringVarP(&cc.endpoint, EndpointFlag, "e", "", "the create result reporting address. It takes effect only when the async value is true and the value is not empty")
	flags.BoolVarP(&cc.nohup, NohupFlag, "n", false, "used to internal async create, no need to config")

	cc.baseExpCommandService = newBaseExpCommandService(cc)
}

func (cc *CreateCommand) bindFlagsFunction() func(commandFlags map[string]func() string, cmd *cobra.Command, specFlags []spec.ExpFlagSpec) {
	return func(commandFlags map[string]func() string, cmd *cobra.Command, specFlags []spec.ExpFlagSpec) {
		// set action flags
		for _, flag := range specFlags {
			flagName := flag.FlagName()
			flagDesc := flag.FlagDesc()
			if flag.FlagRequired() {
				flagDesc = fmt.Sprintf("%s (required)", flagDesc)
			}
			if flag.FlagNoArgs() {
				var key bool
				cmd.PersistentFlags().BoolVar(&key, flagName, false, flagDesc)
				commandFlags[flagName] = func() string {
					return strconv.FormatBool(key)
				}
			} else {
				var key string
				cmd.PersistentFlags().StringVar(&key, flagName, "", flagDesc)
				commandFlags[flagName] = func() string {
					return key
				}
			}
			if flag.FlagRequired() {
				cmd.MarkPersistentFlagRequired(flagName)
			}
		}
	}
}

func (cc *CreateCommand) actionRunEFunc(target, scope string, actionCommand *actionCommand, actionCommandSpec spec.ExpActionCommandSpec) func(cmd *cobra.Command, args []string) error {
	return func(cmd *cobra.Command, args []string) error {
		expModel := createExpModel(target, scope, actionCommandSpec.Name(), cmd)
		expModel.ActionProcessHang = actionCommandSpec.ProcessHang()
		// check timeout flag
		tt := expModel.ActionFlags["timeout"]
		if tt != "" {
			//errNumber checks whether timout flag is parsable as Number
			if _, errNumber := strconv.ParseUint(tt, 10, 64); errNumber != nil {
				//err checks whether timout flag is parsable as Time
				if _, err := time.ParseDuration(tt); err != nil {
					return err
				}
			}
		}
		nohup := expModel.ActionFlags[NohupFlag] == "true"
		var model *data.ExperimentModel
		var resp *spec.Response
		var err error
		if nohup {
			uid := expModel.ActionFlags[UidFlag]
			if uid == "" {
				logrus.Infof("can not execute nohup, uid is null")
				return spec.ResponseFailWithFlags(spec.ParameterLess, UidFlag)
			} else {
				model, err = GetDS().QueryExperimentModelByUid(uid)
				if err == nil {
					delete(expModel.ActionFlags, NohupFlag)
				}
			}
		} else {
			// update status
			model, resp = actionCommand.recordExpModel(cmd.CommandPath(), expModel)
		}
		if resp != nil && !resp.Success {
			return resp
		}
		// is async ?
		async := expModel.ActionFlags[AsyncFlag] == "true"
		endpoint := expModel.ActionFlags[EndpointFlag]

		if async {
			var args string
			if scope == "host" {
				args = fmt.Sprintf("create %s %s --uid %s --nohup=true", target, actionCommand.Name(), model.Uid)
			} else if scope == "docker" || scope == "cri" {
				args = fmt.Sprintf("create %s %s %s --uid %s --nohup=true", scope, target, actionCommand.Name(), model.Uid)
			} else {
				args = fmt.Sprintf("create k8s %s-%s %s --uid %s --nohup=true", scope, target, actionCommand.Name(), model.Uid)
			}
			cmd.Flags().VisitAll(func(flag *pflag.Flag) {
				if flag.Value.String() == "false" {
					return
				}
				if flag.Name == AsyncFlag || flag.Name == UidFlag {
					return
				}
				args = fmt.Sprintf("%s --%s=%s ", args, flag.Name, flag.Value)
			})
			args = fmt.Sprintf("%s %s %s", path.Join(util.GetProgramPath(), "blade"), args, "> /dev/null 2>&1 &")
			response := channel.NewLocalChannel().Run(context.Background(), "nohup", args)
			if response.Success {
				logrus.Infof("async create success, uid: %s", model.Uid)
				cmd.Println(spec.ReturnSuccess(model.Uid).Print())
			} else {
				logrus.Warningf("async create fail, err: %s, uid: %s", response.Err, model.Uid)
				cmd.Println(spec.ResponseFailWithFlags(spec.OsCmdExecFailed, "nohup", response.Err).Print())
			}
			return nil
		} else {
			// execute experiment
			executor := actionCommandSpec.Executor()
			executor.SetChannel(channel.NewLocalChannel())
			response := executor.Exec(model.Uid, context.Background(), expModel)
			response.Result = model.Uid
			if response.Code == spec.ReturnOKDirectly.Code {
				// return directly
				response.Code = spec.OK.Code
				cmd.Println(response.Print())
				endpointCallBack(endpoint, model.Uid, response)
			}
			// pass the uid, expModel to actionCommand
			actionCommand.expModel = expModel
			actionCommand.uid = model.Uid

			if !response.Success {
				// update status
				checkError(GetDS().UpdateExperimentModelByUid(model.Uid, Error, response.Err))
				endpointCallBack(endpoint, model.Uid, response)
				return response
			}
			// update status
			checkError(GetDS().UpdateExperimentModelByUid(model.Uid, Success, response.Err))
			response.Result = model.Uid
			cmd.Println(response.Print())
			endpointCallBack(endpoint, model.Uid, response)
			return nil
		}
	}
}

func endpointCallBack(endpoint, uid string, response *spec.Response) {
	if endpoint != "" {
		logrus.Infof("report response: %s to endpoint: %s", response.Print(), endpoint)
		experimentModel, _ := GetDS().QueryExperimentModelByUid(uid)
		body, err := json.Marshal(experimentModel)
		if err != nil {
			logrus.Warningf("create post body %s failed, %v", response.Print(), err)
		} else {
			result, err, code := util.PostCurl(endpoint, body, "application/json")
			if err != nil {
				logrus.Warningf("report result %s failed, %v", response.Print(), err)
			} else if code != 200 {
				logrus.Warningf("response code is %d, result %s", code, result)
			} else {
				logrus.Infof("report result success, result %s", result)
			}
		}
	}
}

func (cc *CreateCommand) actionPostRunEFunc(actionCommand *actionCommand) func(cmd *cobra.Command, args []string) error {
	return func(cmd *cobra.Command, args []string) error {
		const bladeBin = "blade"
		if actionCommand.expModel != nil {
			tt := actionCommand.expModel.ActionFlags["timeout"]
			async := actionCommand.expModel.ActionFlags[AsyncFlag] == "true"
			if tt == "" || async {
				return nil
			}
			//err possible if timeout used as timeDuration.
			timeout, err := strconv.ParseUint(tt, 10, 64)

			if err != nil {
				// the err checked in RunE function
				timeDuartion, _ := time.ParseDuration(tt)
				timeout = uint64(timeDuartion.Seconds())
			}

			if timeout > 0 && actionCommand.uid != "" {
				// fix https://github.com/chaosblade-io/chaosblade-operator/issues/34
				if actionCommand.expModel.Scope == "container" || actionCommand.expModel.Scope == "pod" {
					timeout = timeout + 60
				}
				script := path.Join(util.GetProgramPath(), bladeBin)
				args := fmt.Sprintf("nohup /bin/sh -c 'sleep %d; %s destroy %s' > /dev/null 2>&1 &",
					timeout, script, actionCommand.uid)
				cmd := exec.CommandContext(context.TODO(), "/bin/sh", "-c", args)
				return cmd.Run()
			}
		}
		return nil
	}
}

func createExample() string {
	return `blade create cpu load --cpu-percent 60`
}
