package cmd

import (
	"context"
	"fmt"
	"os/exec"
	"path"
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
				return err
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
			checkError(GetDS().UpdateExperimentModelByUid(model.Uid, "Error", response.Err))
			return response
		}
		// update status
		checkError(GetDS().UpdateExperimentModelByUid(model.Uid, "Success", response.Err))
		response.Result = model.Uid
		cmd.Println(response.Print())
		return nil
	}
}

func (cc *CreateCommand) actionPostRunEFunc(actionCommand *actionCommand) func(cmd *cobra.Command, args []string) error {
	return func(cmd *cobra.Command, args []string) error {
		const bladeBin = "blade"
		if actionCommand.expModel != nil {
			tt := actionCommand.expModel.ActionFlags["timeout"]
			if tt == "" {
				return nil
			}
			// the err checked in RunE function
			if timeout, _ := strconv.ParseUint(tt, 10, 64); timeout > 0 && actionCommand.uid != "" {
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
