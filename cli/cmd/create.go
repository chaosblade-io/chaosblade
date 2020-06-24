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
	"fmt"
	"os/exec"
	"path"
	"regexp"
	"strconv"

	"github.com/chaosblade-io/chaosblade-spec-go/channel"
	"github.com/chaosblade-io/chaosblade-spec-go/spec"
	"github.com/chaosblade-io/chaosblade-spec-go/util"
	"github.com/spf13/cobra"
)

// CreateCommand for create experiment
type CreateCommand struct {
	baseCommand
	*baseExpCommandService
}

const UidFlag = "uid"

//SecondsInMinute is Number of Seconds in Minute
const SecondsInMinute uint64 = 60

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
	flags.StringVar(&uid, UidFlag, "", "Set Uid for the experiment, adapt to docker")

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

		// check timeout flag
		tt := expModel.ActionFlags["timeout"]
		if tt != "" {
			_, err := strconv.ParseUint(tt, 10, 64)
			if err != nil {
				// regexTest is compiled to test the input for timeInterval format [like 2m33s, 1h3m2s or 43s].
				regexTest, _ := regexp.Compile("^(\\d+h)?(\\d+m)?(\\d+s)?$")
				if regexTest.MatchString(tt) {

				} else {
					return err
				}
			}
		}

		// update status
		model, err := actionCommand.recordExpModel(cmd.CommandPath(), expModel)
		if err != nil {
			return spec.ReturnFail(spec.Code[spec.DatabaseError], err.Error())
		}

		// execute experiment
		executor := actionCommandSpec.Executor()
		executor.SetChannel(channel.NewLocalChannel())
		response := executor.Exec(model.Uid, context.Background(), expModel)

		// pass the uid, expModel to actionCommand
		actionCommand.expModel = expModel
		actionCommand.uid = model.Uid

		if !response.Success {
			// update status
			checkError(GetDS().UpdateExperimentModelByUid(model.Uid, Error, response.Err))
			return response
		}
		// update status
		checkError(GetDS().UpdateExperimentModelByUid(model.Uid, Success, response.Err))
		response.Result = model.Uid
		cmd.Println(response.Print())
		return nil
	}
}

// getTimeInSeconds converts string to uint64 [3m => 180, 34s => 34].
func getTimeInSeconds(timeInterval string) uint64 {
	length := len(timeInterval)
	var count uint64 = 0
	if numericValue, err := strconv.ParseUint(timeInterval[:length-1], 10, 64); err == nil {
		switch timeInterval[length-1] {
		case 104: //ASCII value of "h".
			count += numericValue * (SecondsInMinute * SecondsInMinute)

		case 109: //ASCII value of "m".
			count += numericValue * SecondsInMinute

		case 115: //ASCII value of "s".
			count += numericValue
		}
	}
	return count
}

// timeInStringsToSeconds converts string to unit4 [like  1h34m23s=>5663, 2h34s=>7234 , 34m=>2040(similar)].
func timeInStringToSeconds(time string) uint64 {
	// regexGroups the key time intervals to substrings.
	regexGroups, _ := regexp.Compile("([\\d]+[h,m,s])")

	var seconds uint64 = 0

	// values stores the substrings as an array.
	values := regexGroups.FindAllString(time, -1)

	for i := 0; i < len(values); i++ {
		seconds += getTimeInSeconds(values[i])
	}

	return seconds
}

func (cc *CreateCommand) actionPostRunEFunc(actionCommand *actionCommand) func(cmd *cobra.Command, args []string) error {
	return func(cmd *cobra.Command, args []string) error {
		const bladeBin = "blade"
		if actionCommand.expModel != nil {
			tt := actionCommand.expModel.ActionFlags["timeout"]
			if tt == "" {
				return nil
			}

			timeout, err := strconv.ParseUint(tt, 10, 64)

			//err possible if timeout used as timeInterval.
			if err != nil {
				timeout = timeInStringToSeconds(tt)
			}
			// the err checked in RunE function
			if timeout > 0 && actionCommand.uid != "" {
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
