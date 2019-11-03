package cmd

import (
	"fmt"
	"strings"
	"time"

	"github.com/chaosblade-io/chaosblade/data"
	"github.com/chaosblade-io/chaosblade-spec-go/util"

	"github.com/spf13/cobra"
	"github.com/chaosblade-io/chaosblade-spec-go/spec"
)

// Command is cli command interface
type Command interface {
	// Init command
	Init()

	// CobraCmd
	CobraCmd() *cobra.Command

	// Name
	Name() string
}

// baseCommand
type baseCommand struct {
	command *cobra.Command
}

func (bc *baseCommand) Init() {
}

func (bc *baseCommand) CobraCmd() *cobra.Command {
	return bc.command
}

func (bc *baseCommand) Name() string {
	return bc.command.Name()
}

var ds data.SourceI

// GetDS returns dataSource
func GetDS() data.SourceI {
	if ds == nil {
		ds = data.GetSource()
	}
	return ds
}

// SetDS for test
func SetDS(source data.SourceI) {
	ds = source
}

// recordExpModel
func (bc *baseCommand) recordExpModel(commandPath string, expModel *spec.ExpModel) (commandModel *data.ExperimentModel, err error) {
	uid := expModel.ActionFlags[UidFlag]
	if uid == "" {
		uid, err = bc.generateUid()
		if err != nil {
			return nil, err
		}
	}

	flagsInline := spec.ConvertExpMatchersToString(expModel, func() map[string]spec.Empty {
		return make(map[string]spec.Empty)
	})
	time := time.Now().Format(time.RFC3339Nano)
	command, subCommand, err := parseCommandPath(commandPath)
	if err != nil {
		return nil, err
	}
	commandModel = &data.ExperimentModel{
		Uid:        uid,
		Command:    command,
		SubCommand: subCommand,
		Flag:       flagsInline,
		Status:     "Created",
		Error:      "",
		CreateTime: time,
		UpdateTime: time,
	}
	err = GetDS().InsertExperimentModel(commandModel)
	if err != nil {
		return nil, err
	}
	return commandModel, nil
}

func parseCommandPath(commandPath string) (string, string, error) {
	// chaosbd create docker cpu fullload
	cmds := strings.SplitN(commandPath, " ", 4)
	if len(cmds) < 4 {
		return "", "", fmt.Errorf("not illegal command")
	}
	return cmds[2], cmds[3], nil
}

func (bc *baseCommand) generateUid() (string, error) {
	uid, err := util.GenerateUid()
	if err != nil {
		return "", err
	}
	model, err := GetDS().QueryExperimentModelByUid(uid)
	if err != nil {
		return "", err
	}
	if model == nil {
		return uid, nil
	}
	return bc.generateUid()
}

//AddCommand is add child command to the parent command
func (bc *baseCommand) AddCommand(child Command) {
	child.Init()
	childCmd := child.CobraCmd()
	childCmd.PreRun = func(cmd *cobra.Command, args []string) {
		util.InitLog(util.Blade)
	}
	childCmd.SilenceUsage = true
	childCmd.DisableFlagsInUseLine = true
	childCmd.SilenceErrors = true
	bc.CobraCmd().AddCommand(childCmd)
}
