package cmd

import (
	"context"
	"fmt"
	"path"
	"strconv"
	"sync"

	specutil "github.com/chaosblade-io/chaosblade-spec-go/util"
	"github.com/chaosblade-io/chaosblade/exec/cplus"
	"github.com/chaosblade-io/chaosblade/exec/docker"
	"github.com/chaosblade-io/chaosblade/exec/jvm"
	"github.com/chaosblade-io/chaosblade-spec-go/spec"
	"github.com/chaosblade-io/chaosblade-spec-go/util"
	"github.com/sirupsen/logrus"
	"github.com/spf13/cobra"
	"github.com/spf13/pflag"
	"github.com/chaosblade-io/chaosblade-spec-go/channel"
	"os/exec"
	"github.com/chaosblade-io/chaosblade/version"
	"github.com/chaosblade-io/chaosblade/exec/os"
	"github.com/chaosblade-io/chaosblade/exec/kubernetes"
)

// ExpActionFlags is used to receive experiment action flags
type ExpActionFlags struct {
	// ActionFlags cache action flags, contains name key and description value
	ActionFlags map[string]func() string

	// MatcherFlags cache matcher flags, contains name key and description value
	MatcherFlags map[string]func() string
}

// expFlags is used to receive experiment flags
type ExpFlags struct {
	// Target is experiment target, for example dubbo
	Target string

	// Scope
	Scope string

	// Actions cache action name and flags
	Actions map[string]*ExpActionFlags

	// CommandFlags
	CommandFlags map[string]func() string
}

// modelCommand is the target command
type modelCommand struct {
	baseCommand
	*ExpFlags
}

// actionCommand is action command
type actionCommand struct {
	baseCommand
	*ExpActionFlags
	uid      string
	expModel *spec.ExpModel
}

// modelCommands cache model commands
var modelCommands map[string]*modelCommand
var lock = sync.RWMutex{}

type expCommand struct {
	commands  map[string]*modelCommand
	executors map[string]spec.Executor
}

func NewExpCommand() *expCommand {
	command := &expCommand{
		commands:  make(map[string]*modelCommand, 0),
		executors: make(map[string]spec.Executor, 0),
	}
	command.init()
	return command
}

func (ec *expCommand) init() {
	// register os type command
	ec.registerOsExpCommands()
	// register jvm framework commands
	ec.registerJvmExpCommands()
	// register cplus
	ec.registerCplusExpCommands()
	// register docker command
	ec.registerDockerExpCommands()
	// register k8s command
	ec.registerK8sExpCommands()
}

func (ec *expCommand) AddCommandTo(parent Command) {
	for _, command := range ec.commands {
		parent.CobraCmd().AddCommand(command.CobraCmd())
	}
}

var channel_ spec.Channel = channel.NewLocalChannel()
var ctx_ = context.Background()

// registerOsExpCommands
func (ec *expCommand) registerOsExpCommands() []*modelCommand {
	file := path.Join(util.GetBinPath(), fmt.Sprintf("chaosblade-os-spec-%s.yaml", version.Ver))
	models, err := specutil.ParseSpecsToModel(file, os.NewExecutor())
	if err != nil {
		return nil
	}
	osCommands := make([]*modelCommand, 0)
	for idx := range models.Models {
		model := &models.Models[idx]
		command := ec.registerExpCommand(model, "")
		osCommands = append(osCommands, command)
	}
	return osCommands
}

// registerJvmExpCommands
func (ec *expCommand) registerJvmExpCommands() []*modelCommand {
	file := path.Join(util.GetBinPath(), fmt.Sprintf("chaosblade-jvm-spec-%s.yaml", version.Ver))
	models, err := util.ParseSpecsToModel(file, jvm.NewExecutor())
	if err != nil {
		return nil
	}
	jvmCommands := make([]*modelCommand, 0)
	for idx := range models.Models {
		model := &models.Models[idx]
		command := ec.registerExpCommand(model, "")
		jvmCommands = append(jvmCommands, command)
	}
	return jvmCommands
}

// registerCplusExpCommands
func (ec *expCommand) registerCplusExpCommands() []*modelCommand {
	file := path.Join(util.GetBinPath(), fmt.Sprintf("chaosblade-cplus-spec-%s.yaml", version.Ver))
	models, err := util.ParseSpecsToModel(file, cplus.NewExecutor())
	if err != nil {
		return nil
	}
	cplusCommands := make([]*modelCommand, 0)
	for idx := range models.Models {
		model := &models.Models[idx]
		command := ec.registerExpCommand(model, "")
		cplusCommands = append(cplusCommands, command)
	}
	return cplusCommands
}

// registerDockerExpCommands
func (ec *expCommand) registerDockerExpCommands() []*modelCommand {
	file := path.Join(util.GetBinPath(), fmt.Sprintf("chaosblade-docker-spec-%s.yaml", version.Ver))
	models, err := specutil.ParseSpecsToModel(file, docker.NewExecutor())
	if err != nil {
		return nil
	}
	dockerSpec := docker.NewCommandModelSpec()
	modelCommands := make([]*modelCommand, 0)
	for idx := range models.Models {
		model := &models.Models[idx]
		command := ec.registerExpCommand(model, dockerSpec.Name())
		modelCommands = append(modelCommands, command)
	}
	dockerCmd := ec.registerExpCommand(dockerSpec, "")
	cobraCmd := dockerCmd.CobraCmd()
	for _, child := range modelCommands {
		copyAndAddCommand(cobraCmd, child.command)
	}
	return modelCommands
}

func (ec *expCommand) registerK8sExpCommands() []*modelCommand {
	// 读取 k8s 下的场景并注册
	file := path.Join(util.GetBinPath(), fmt.Sprintf("chaosblade-k8s-spec-%s.yaml", version.Ver))
	models, err := specutil.ParseSpecsToModel(file, kubernetes.NewExecutor())
	if err != nil {
		return nil
	}
	k8sSpec := kubernetes.NewCommandModelSpec()
	modelCommands := make([]*modelCommand, 0)
	for idx := range models.Models {
		model := &models.Models[idx]
		command := ec.registerExpCommand(model, k8sSpec.Name())
		modelCommands = append(modelCommands, command)
	}
	k8sCmd := ec.registerExpCommand(k8sSpec, "")
	cobraCmd := k8sCmd.CobraCmd()

	for _, child := range modelCommands {
		copyAndAddCommand(cobraCmd, child.command)
	}
	return modelCommands
}

// registerExpCommand
func (ec *expCommand) registerExpCommand(commandSpec spec.ExpModelCommandSpec, parentTargetCmd string) *modelCommand {
	cmdName := commandSpec.Name()
	if commandSpec.Scope() != "" && commandSpec.Scope() != "host" && commandSpec.Scope() != "docker" {
		cmdName = fmt.Sprintf("%s-%s", commandSpec.Scope(), commandSpec.Name())
	}
	cmd := &cobra.Command{
		Use:     cmdName,
		Short:   commandSpec.ShortDesc(),
		Long:    commandSpec.LongDesc(),
		Example: commandSpec.Example(),
		RunE: func(cmd *cobra.Command, args []string) error {
			return spec.ReturnFail(spec.Code[spec.IllegalParameters], "less action command")
		},
	}
	// create the experiment command
	command := &modelCommand{
		baseCommand{
			command: cmd,
		},
		&ExpFlags{
			Target:       commandSpec.Name(),
			Scope:        commandSpec.Scope(),
			Actions:      make(map[string]*ExpActionFlags, 0),
			CommandFlags: make(map[string]func() string, 0),
		},
	}
	// add command flags
	ec.bindFlags(command.CommandFlags, cmd, commandSpec.Flags())
	// add action to command
	for idx := range commandSpec.Actions() {
		action := commandSpec.Actions()[idx]
		actionCommand := ec.registerActionCommand(commandSpec.Name(), commandSpec.Scope(), action)
		command.Actions[action.Name()] = actionCommand.ExpActionFlags
		command.AddCommand(actionCommand)
		executor := action.Executor()
		executor.SetChannel(channel.NewLocalChannel())
		ec.executors[createExecutorKey(parentTargetCmd, cmdName, action.Name())] = executor
	}

	if parentTargetCmd == "" {
		// cache command
		ec.commands[cmdName] = command
	}
	return command
}

// registerActionCommand
func (ec *expCommand) registerActionCommand(target, scope string, actionCommandSpec spec.ExpActionCommandSpec) *actionCommand {
	command := &actionCommand{
		baseCommand{},
		&ExpActionFlags{
			ActionFlags:  make(map[string]func() string, 0),
			MatcherFlags: make(map[string]func() string, 0),
		}, "", nil,
	}
	command.command = &cobra.Command{
		Use:     actionCommandSpec.Name(),
		Aliases: actionCommandSpec.Aliases(),
		Short:   actionCommandSpec.ShortDesc(),
		Long:    actionCommandSpec.LongDesc(),
		RunE: func(cmd *cobra.Command, args []string) error {
			return command.runActionCommand(target, scope, cmd, args, actionCommandSpec)
		},
		PostRunE: func(cmd *cobra.Command, args []string) error {
			const bladeBin = "blade"
			if command.expModel != nil {
				tt := command.expModel.ActionFlags["timeout"]
				if tt == "" {
					return nil
				}
				// the err checked in RunE function
				if timeout, _ := strconv.ParseUint(tt, 10, 64); timeout > 0 && command.uid != "" {
					script := path.Join(util.GetProgramPath(), bladeBin)
					args := fmt.Sprintf("nohup /bin/sh -c 'sleep %d; %s destroy %s' > /dev/null 2>&1 &",
						timeout, script, command.uid)
					cmd := exec.CommandContext(context.TODO(), "/bin/sh", "-c", args)
					return cmd.Run()
				}
			}
			return nil
		},
	}

	flags := addTimeoutFlag(actionCommandSpec.Flags())
	ec.bindFlags(command.ActionFlags, command.command, flags)
	// set matcher flags
	ec.bindFlags(command.MatcherFlags, command.command, actionCommandSpec.Matchers())

	return command
}
func addTimeoutFlag(flags []spec.ExpFlagSpec) []spec.ExpFlagSpec {
	contains := false
	for _, flag := range flags {
		if flag.FlagName() == "timeout" {
			contains = true
			break
		}
	}
	if !contains {
		// set action flags, always add timeout param
		flags = append(flags,
			&spec.ExpFlag{
				Name:     "timeout",
				Desc:     "set timeout for experiment in seconds",
				Required: false,
			},
		)
	}
	return flags
}

// runActionCommand
func (command *actionCommand) runActionCommand(target, scope string, cmd *cobra.Command, args []string, actionCommandSpec spec.ExpActionCommandSpec) error {
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
	model, err := command.recordExpModel(cmd.CommandPath(), expModel)
	if err != nil {
		return spec.ReturnFail(spec.Code[spec.DatabaseError], err.Error())
	}

	// execute experiment
	executor := actionCommandSpec.Executor()
	executor.SetChannel(channel_)
	response := executor.Exec(model.Uid, ctx_, expModel)

	// pass the uid, expModel to actionCommand
	command.expModel = expModel
	command.uid = model.Uid

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

// checkError for db operation
func checkError(err error) {
	if err != nil {
		logrus.Warningf(err.Error())
	}
}

// bindFlags
func (ec *expCommand) bindFlags(commandFlags map[string]func() string, cmd *cobra.Command, specFlags []spec.ExpFlagSpec) {
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

func createExpModel(target, scope, actionName string, cmd *cobra.Command) *spec.ExpModel {
	expModel := &spec.ExpModel{
		Target:      target,
		Scope:       scope,
		ActionName:  actionName,
		ActionFlags: make(map[string]string, 0),
	}

	cmd.Flags().VisitAll(func(flag *pflag.Flag) {
		if flag.Value.String() == "false" {
			return
		}
		expModel.ActionFlags[flag.Name] = flag.Value.String()
	})
	return expModel
}

// getExecutor from expCommand executors cache
func (ec *expCommand) getExecutor(target, actionTarget, action string) spec.Executor {
	key := createExecutorKey(target, actionTarget, action)
	return ec.executors[key]
}

func createExecutorKey(target, actionTarget, action string) string {
	key := target
	arr := []string{actionTarget, action}
	for _, str := range arr {
		if str != "" {
			if key != "" {
				key = fmt.Sprintf("%s-%s", key, str)
			} else {
				key = str
			}
		}
	}
	return key
}

// copyAndAddCommand for add basic experiment to parent
func copyAndAddCommand(parent, child *cobra.Command) {
	var newChild = &cobra.Command{}
	*newChild = *child
	newChild.ResetCommands()
	parent.AddCommand(newChild)
	if len(child.Commands()) == 0 {
		return
	}
	commands := child.Commands()
	for _, command := range commands {
		copyAndAddCommand(newChild, command)
	}
}
