package main

import (
	"context"
	"fmt"
	osexec "os/exec"
	"path"
	"strconv"
	"sync"

	"github.com/chaosblade-io/chaosblade/exec"
	"github.com/chaosblade-io/chaosblade/exec/docker"
	"github.com/chaosblade-io/chaosblade/exec/jvm"
	"github.com/chaosblade-io/chaosblade/exec/kubernetes"
	"github.com/chaosblade-io/chaosblade/exec/os"
	"github.com/chaosblade-io/chaosblade/transport"
	"github.com/chaosblade-io/chaosblade/util"
	"github.com/sirupsen/logrus"
	"github.com/spf13/cobra"
	"github.com/spf13/pflag"
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
	expModel *exec.ExpModel
}

// modelCommands cache model commands
var modelCommands map[string]*modelCommand
var lock = sync.RWMutex{}

type expCommand struct {
	commands     map[string]*modelCommand
	executors    map[string]exec.Executor
	preExecutors map[string]exec.PreExecutor
}

func NewExpCommand() *expCommand {
	command := &expCommand{
		commands:     make(map[string]*modelCommand, 0),
		executors:    make(map[string]exec.Executor, 0),
		preExecutors: make(map[string]exec.PreExecutor, 0),
	}
	command.init()
	return command
}

func (ec *expCommand) init() {
	// register os type command
	osExpCommands := ec.registerOsExpCommands()
	// register jvm framework commands
	ec.registerJvmExpCommands()
	// register docker command
	ec.registerDockerExpCommands(osExpCommands)
	// register k8s command
	ec.registerK8sExpCommands()
}

func (ec *expCommand) AddCommandTo(parent Command) {
	for _, command := range ec.commands {
		parent.CobraCmd().AddCommand(command.CobraCmd())
	}
}

var channel_ exec.Channel = exec.NewLocalChannel()
var ctx_ = context.Background()

// registerOsExpCommands
func (ec *expCommand) registerOsExpCommands() []*modelCommand {
	// register cpu
	cpu := ec.registerExpCommand(&os.CpuCommandModelSpec{})
	process := ec.registerExpCommand(&os.ProcessCommandModelSpec{})
	network := ec.registerExpCommand(&os.NetworkCommandSpec{})
	disk := ec.registerExpCommand(&os.DiskCommandSpec{})
	return []*modelCommand{
		cpu,
		process,
		network,
		disk,
	}
}

// registerJvmExpCommands
func (ec *expCommand) registerJvmExpCommands() []*modelCommand {
	file := path.Join(util.GetBinPath(), "jvm.spec.yaml")
	models, err := exec.ParseSpecsToModel(file, jvm.NewExecutor())
	if err != nil {
		return nil
	}
	jvmCommands := make([]*modelCommand, 0)
	for idx := range models.Models {
		model := &models.Models[idx]
		command := ec.registerExpCommand(model)
		jvmCommands = append(jvmCommands, command)
	}
	return jvmCommands
}

// registerDockerExpCommands
func (ec *expCommand) registerDockerExpCommands(commands ...[]*modelCommand) {
	spec := &docker.CommandModelSpec{}
	dockerCmd := ec.registerExpCommand(spec)
	cobraCmd := dockerCmd.CobraCmd()
	// add PersistentPreRunE
	cobraCmd.PersistentPreRunE = func(cmd *cobra.Command, args []string) error {
		return runDockerPre(cmd, args, spec)
	}
	for _, cmds := range commands {
		for _, child := range cmds {
			copyAndAddCommand(cobraCmd, child.command)
		}
	}
}

// runDockerPre
func runDockerPre(cmd *cobra.Command, args []string, spec *docker.CommandModelSpec) error {
	parentCmdName := ""
	if cmd.Parent() != nil {
		parentCmdName = cmd.Parent().Name()
	}
	flags := make(map[string]string, 0)
	cmd.Flags().VisitAll(func(flag *pflag.Flag) {
		flags[flag.Name] = flag.Value.String()
	})
	// default channel is local channel
	preExec := spec.PreExecutor().PreExec(cmd.Name(), parentCmdName, flags)
	if preExec == nil {
		return nil
	}
	channel, ctx, err := preExec(context.Background())
	if err != nil {
		return transport.ReturnFail(transport.Code[transport.PreHandleError], err.Error())
	}
	if channel != nil {
		channel_ = channel
	}
	ctx_ = ctx
	return nil
}

func (ec *expCommand) registerK8sExpCommands(commands ...[]*modelCommand) {
	spec := &kubernetes.CommandModelSpec{}
	k8sCmd := ec.registerExpCommand(spec)
	// add os and jvm command to k8s
	cobraCmd := k8sCmd.CobraCmd()
	cobraCmd.PersistentPreRunE = func(cmd *cobra.Command, args []string) error {
		return runK8sPre(cmd, args, spec)
	}
	for _, cmds := range commands {
		for _, child := range cmds {
			copyAndAddCommand(cobraCmd, child.command)
		}
	}
}
func runK8sPre(cmd *cobra.Command, args []string, spec *kubernetes.CommandModelSpec) error {
	parentCmdName := ""
	if cmd.Parent() != nil {
		parentCmdName = cmd.Parent().Name()
	}
	flags := make(map[string]string, 0)
	cmd.Flags().VisitAll(func(flag *pflag.Flag) {
		flags[flag.Name] = flag.Value.String()
	})
	preExec := spec.PreExecutor().PreExec(cmd.Name(), parentCmdName, flags)
	if preExec == nil {
		return nil
	}
	channel, ctx, err := preExec(context.Background())
	if err != nil {
		return transport.ReturnFail(transport.Code[transport.PreHandleError], err.Error())
	}
	channel_ = channel
	ctx_ = ctx
	return nil
}

// registerExpCommand
func (ec *expCommand) registerExpCommand(spec exec.ExpModelCommandSpec) *modelCommand {
	cmd := &cobra.Command{
		Use:     spec.Name(),
		Short:   spec.ShortDesc(),
		Long:    spec.LongDesc(),
		Example: spec.Example(),
		RunE: func(cmd *cobra.Command, args []string) error {
			return transport.ReturnFail(transport.Code[transport.IllegalParameters], "less action command")
		},
	}
	// create the experiment command
	command := &modelCommand{
		baseCommand{
			command: cmd,
		},
		&ExpFlags{
			Target:       spec.Name(),
			Actions:      make(map[string]*ExpActionFlags, 0),
			CommandFlags: make(map[string]func() string, 0),
		},
	}
	// add command flags
	ec.bindFlags(command.CommandFlags, cmd, spec.Flags())
	// add action to command
	for idx := range spec.Actions() {
		action := spec.Actions()[idx]
		actionCommand := ec.registerActionCommand(spec.Name(), action)
		command.Actions[action.Name()] = actionCommand.ExpActionFlags
		command.AddCommand(actionCommand)
		ec.executors[createExecutorKey(spec.Name(), action.Name())] = action.Executor(exec.NewLocalChannel())
	}
	// cache command
	ec.commands[spec.Name()] = command
	// cache preExecutor
	ec.preExecutors[spec.Name()] = spec.PreExecutor()
	return command
}

// registerActionCommand
func (ec *expCommand) registerActionCommand(actionParentCmdName string, spec exec.ExpActionCommandSpec) *actionCommand {
	command := &actionCommand{
		baseCommand{},
		&ExpActionFlags{
			ActionFlags:  make(map[string]func() string, 0),
			MatcherFlags: make(map[string]func() string, 0),
		}, "", nil,
	}
	command.command = &cobra.Command{
		Use:     spec.Name(),
		Aliases: spec.Aliases(),
		Short:   spec.ShortDesc(),
		Long:    spec.LongDesc(),
		RunE: func(cmd *cobra.Command, args []string) error {
			return command.runActionCommand(actionParentCmdName, cmd, args, spec)
		},
		PostRunE: func(cmd *cobra.Command, args []string) error {
			const bladeBin = "blade"

			if command.expModel != nil {
				if timeout, err := strconv.ParseUint(command.expModel.ActionFlags["timeout"], 10, 64); err == nil && timeout > 0 && command.uid != "" {
					script := path.Join(util.GetProgramPath(), bladeBin)
					args := fmt.Sprintf("nohup /bin/sh -c 'sleep %d; %s destroy %s' > /dev/null 2>&1 &",
						timeout, script, command.uid)
					cmd := osexec.CommandContext(context.TODO(), "/bin/sh", "-c", args)
					return cmd.Run()
				} else {
					return err
				}
			}

			return nil
		},
	}

	// set action flags, always add timeout param
	// @TODO `timeout` param does not list in cobra params
	flags := append(spec.Flags(),
		&exec.ExpFlag{
			Name:     "timeout",
			Desc:     "set timeout for experiment",
			Required: false,
		},
	)
	ec.bindFlags(command.ActionFlags, command.command, flags)
	// set matcher flags
	ec.bindFlags(command.MatcherFlags, command.command, spec.Matchers())

	return command
}

// runActionCommand
func (command *actionCommand) runActionCommand(actionParentCmdName string, cmd *cobra.Command, args []string, spec exec.ExpActionCommandSpec) error {
	expModel := createExpModel(actionParentCmdName, spec.Name(), cmd)
	// update status
	model, err := command.recordExpModel(cmd.CommandPath(), expModel.GetFlags())
	if err != nil {
		return transport.ReturnFail(transport.Code[transport.DatabaseError], err.Error())
	}

	// execute experiment
	executor := spec.Executor(channel_)
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
func (ec *expCommand) bindFlags(commandFlags map[string]func() string, cmd *cobra.Command, specFlags []exec.ExpFlagSpec) {
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
			// @TODO dont convert EVERYTHING into string
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

func createExpModel(actionParentName, actionName string, cmd *cobra.Command) *exec.ExpModel {
	expModel := &exec.ExpModel{
		Target:      actionParentName,
		ActionName:  actionName,
		ActionFlags: make(map[string]string, 0),
	}

	cmd.Flags().VisitAll(func(flag *pflag.Flag) {
		expModel.ActionFlags[flag.Name] = flag.Value.String()
	})
	return expModel
}

// getExecutor from expCommand executors cache
func (ec *expCommand) getExecutor(target, action string) exec.Executor {
	key := createExecutorKey(target, action)
	return ec.executors[key]
}

func createExecutorKey(target, action string) string {
	return fmt.Sprintf("%s-%s", target, action)
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
