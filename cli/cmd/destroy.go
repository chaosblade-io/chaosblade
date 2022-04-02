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
	"github.com/chaosblade-io/chaosblade-spec-go/log"
	"strconv"
	"strings"

	"github.com/chaosblade-io/chaosblade-operator/pkg/apis/chaosblade/v1alpha1"
	"github.com/chaosblade-io/chaosblade-spec-go/channel"
	"github.com/chaosblade-io/chaosblade-spec-go/spec"
	"github.com/spf13/cobra"

	"github.com/chaosblade-io/chaosblade/data"
	"github.com/chaosblade-io/chaosblade/exec/kubernetes"
)

const (
	ForceRemoveFlag = "force-remove"
	ExpTargetFlag   = "target"
	KubeconfigFlag  = "kubeconfig"
)

type DestroyCommand struct {
	baseCommand
	*baseExpCommandService
	forceRemove           bool
	expTarget, kubeconfig string
}

func (dc *DestroyCommand) Init() {
	dc.command = &cobra.Command{
		Use:     "destroy UID",
		Short:   "Destroy a chaos experiment",
		Long:    "Destroy a chaos experiment by experiment uid which you can run status command to query",
		Args:    cobra.MinimumNArgs(1),
		Aliases: []string{"d"},
		Example: destroyExample(),
		RunE: func(cmd *cobra.Command, args []string) error {
			return dc.runDestroyWithUid(context.Background(), cmd, args)
		},
	}
	flags := dc.command.PersistentFlags()
	flags.StringVar(&uid, UidFlag, "", "Set Uid for the experiment, adapt to container")
	flags.StringVar(&dc.expTarget, ExpTargetFlag, "", "Specify experiment target, such as --target k8s. Used to destroy creating k8s experiments without using blade command")
	flags.BoolVar(&dc.forceRemove, ForceRemoveFlag, false, "Force remove chaosblade resource or record even if destroy experiment failed")
	flags.StringVar(&dc.kubeconfig, KubeconfigFlag, "", "The config file of kubernetes cluster. Used to destroy creating k8s experiments without using blade command")
	dc.baseExpCommandService = newBaseExpCommandService(dc)
}

// runDestroyWithUid destroy and remove experiment based on Uid and forceRemoveFlag
// Processes k8s experiments not only local records, but also chaosblade resources in the cluster.
func (dc *DestroyCommand) runDestroyWithUid(ctx context.Context, cmd *cobra.Command, args []string) error {
	uid := args[0]
	log.Infof(ctx, "destroy by %s uid, force-remove: %t, target: %s", uid, dc.forceRemove, dc.expTarget)
	model, err := GetDS().QueryExperimentModelByUid(uid)
	lowerExpTarget := strings.ToLower(dc.expTarget)
	isK8sTarget := lowerExpTarget == "kubernetes" || lowerExpTarget == "k8s"
	if err != nil || model == nil {
		if isK8sTarget {
			return dc.destroyAndRemoveK8sExperimentWithoutRecordByForceFlag(cmd, uid)
		}
		if err != nil {
			return spec.ResponseFailWithFlags(spec.DatabaseError, "query", err)
		}
		return spec.ResponseFailWithFlags(spec.DataNotFound, uid)
	}
	return dc.destroyAndRemoveExperimentByUidAndForceFlag(cmd, err, model, uid, isK8sTarget)
}

// destroyAndRemoveK8sExperimentWithoutRecordByForceFlag deletes and forcibly removes the chaosblade resources in the cluster by the forceRemoveFlag.
func (dc *DestroyCommand) destroyAndRemoveK8sExperimentWithoutRecordByForceFlag(cmd *cobra.Command, uid string) error {
	response, err := dc.destroyK8sExperimentWithoutRecord(uid)
	removeResourceErr := dc.checkAndForceRemoveForK8sExp(uid, dc.kubeconfig)
	if err == nil && removeResourceErr == nil {
		cmd.Println(response.Print())
		return nil
	}
	if err == nil && removeResourceErr != nil {
		return spec.ResponseFailWithFlags(spec.DatabaseError, "remove",
			fmt.Sprintf("the %s has been destroyed, but forcibly remove resource failed, %v", uid, removeResourceErr))
	}
	response = err.(*spec.Response)
	if dc.forceRemove && removeResourceErr == nil {
		response.Err = fmt.Sprintf("forcibly remove %s resource success, but destroy the experiment failed, %s", uid, response.Err)
		return response
	}
	if dc.forceRemove {
		response.Err = fmt.Sprintf("destroy and forcibly remove the %s experiment failed, %s, %v", uid, response.Err, removeResourceErr)
	}
	return response
}

// destroyAndRemoveExperimentByUidAndForceFlag destroys and forcibly deletes experiments with local records, including k8s experiments.
func (dc *DestroyCommand) destroyAndRemoveExperimentByUidAndForceFlag(
	cmd *cobra.Command, err error, model *data.ExperimentModel, uid string, isK8sTarget bool) error {
	response, err := dc.destroyExperimentByUid(model, uid)
	removeRecordErr := dc.checkAndForceRemoveForExpRecord(uid)
	var removeResourceErr error
	if isK8sTarget {
		removeResourceErr = dc.checkAndForceRemoveForK8sExp(uid, dc.kubeconfig)
	}
	if err == nil {
		if removeRecordErr == nil && removeResourceErr == nil {
			cmd.Println(response.Print())
			return nil
		}
		return spec.ResponseFailWithFlags(spec.DatabaseError, "remove",
			fmt.Sprintf("the %s has been destroyed, but forcibly remove resource failed, %v|%v",
				uid, removeRecordErr, removeResourceErr))
	}
	response = err.(*spec.Response)
	if dc.forceRemove && removeRecordErr == nil && removeResourceErr == nil {
		response.Err = fmt.Sprintf("forcibly remove %s resource success, but destroy the experiment failed, %s", uid, response.Err)
		return response
	}
	if dc.forceRemove {
		response.Err = fmt.Sprintf("destroy and forcibly remove the %s experiment failed, %s, %v|%v",
			uid, response.Err, removeRecordErr, removeResourceErr)
	}
	return response
}

// destroyExperimentByUid destroys experiments with local records, including k8s experiments.
func (dc *DestroyCommand) destroyExperimentByUid(model *data.ExperimentModel, uid string) (*spec.Response, error) {
	if model == nil {
		return nil, spec.ResponseFailWithFlags(spec.DataNotFound, uid)
	}
	if model.Status == Destroyed {
		result := fmt.Sprintf("command: %s %s %s, destroy time: %s",
			model.Command, model.SubCommand, model.Flag, model.UpdateTime)
		return spec.ReturnSuccess(result), nil
	}
	executor, expModel, err := dc.getExecutorAndExpModelByRecord(model)
	if err != nil {
		return nil, spec.ResponseFailWithFlags(spec.HandlerExecNotFound, err.Error())
	}
	if err = dc.destroyExperiment(uid, executor, expModel); err != nil {
		return nil, err
	}
	return spec.ReturnSuccess(expModel), nil
}

//destroyK8sExperimentWithoutRecord deletes chaosblade resources by name in the cluster.
func (dc *DestroyCommand) destroyK8sExperimentWithoutRecord(uid string) (*spec.Response, error) {
	if uid == "" || dc.kubeconfig == "" {
		return nil, spec.ResponseFailWithFlags(spec.ParameterLess,
			"usage: blade destroy UID --target k8s --kubeconfig KUBECONFIG")
	}
	exp, err := kubernetes.GetChaosBladeByName(uid, dc.kubeconfig)
	if err != nil {
		return nil, spec.ResponseFailWithFlags(spec.K8sExecFailed, "GetChaosBlade", err)
	}
	if exp.Status.Phase == v1alpha1.ClusterPhaseDestroyed {
		return spec.ReturnSuccess(exp), nil
	}
	executor, expModel, err := dc.getExecutorAndExpModelByChaosBladeResource(exp)
	if err != nil {
		return nil, spec.ResponseFailWithFlags(spec.HandlerExecNotFound, err.Error())
	}
	if err := dc.destroyExperiment(uid, executor, expModel); err != nil {
		return nil, err
	}
	return spec.ReturnSuccess(exp), nil
}

// checkAndForceRemoveForK8sExp deletes chaosblade resource by resource name if force-remove is true
func (dc *DestroyCommand) checkAndForceRemoveForK8sExp(name, kubeconfig string) error {
	if dc.forceRemove {
		return kubernetes.RemoveFinalizer(name, kubeconfig)
	}
	return nil
}

// checkAndForceRemoveForExpRecord deletes experiment record by uid if force-remove is true
func (dc *DestroyCommand) checkAndForceRemoveForExpRecord(uid string) error {
	if dc.forceRemove {
		return GetDS().DeleteExperimentModelByUid(uid)
	}
	return nil
}

func (dc *DestroyCommand) destroyExperiment(uid string, executor spec.Executor, expModel *spec.ExpModel) error {
	// set destroy flag
	ctx := spec.SetDestroyFlag(context.Background(), uid)
	ctx = context.WithValue(ctx, spec.Uid, uid)
	// execute
	response := executor.Exec(uid, ctx, expModel)
	if !response.Success {
		return response
	}
	// return result
	checkError(GetDS().UpdateExperimentModelByUid(uid, Destroyed, ""))
	return nil
}

func (dc *DestroyCommand) getExecutorAndExpModelByRecord(model *data.ExperimentModel) (
	executor spec.Executor, expModel *spec.ExpModel, err error) {
	var firstCommand = model.Command
	var actionCommand, actionTargetCommand string
	subCommands := strings.Split(model.SubCommand, " ")
	subLength := len(subCommands)
	if subLength > 0 {
		if subLength > 1 {
			actionCommand = subCommands[subLength-1]
			actionTargetCommand = subCommands[subLength-2]
		} else {
			actionCommand = subCommands[0]
			actionTargetCommand = ""
		}
	}
	executor = dc.GetExecutor(firstCommand, actionTargetCommand, actionCommand)
	if executor == nil {
		err = fmt.Errorf("can't find executor for %s, %s", model.Command, model.SubCommand)
		return
	}
	if actionTargetCommand == "" {
		actionTargetCommand = firstCommand
	}
	// covert commandModel to expModel
	expModel = spec.ConvertCommandsToExpModel(actionCommand, actionTargetCommand, model.Flag)
	return
}

func (dc *DestroyCommand) getExecutorAndExpModelByChaosBladeResource(chaosBlade *v1alpha1.ChaosBlade) (
	executor spec.Executor, expModel *spec.ExpModel, err error) {
	for _, experiment := range chaosBlade.Spec.Experiments {
		actionTarget := fmt.Sprintf("%s-%s", experiment.Scope, experiment.Target)
		executor = dc.GetExecutor("k8s", actionTarget, experiment.Action)
		if executor == nil {
			err = fmt.Errorf("can't find executor for k8s %s, %s", actionTarget, experiment.Action)
			return
		}
		expModel = convertCBExperimentToExpModel(experiment, actionTarget)
		break
	}
	return
}

func convertCBExperimentToExpModel(experiment v1alpha1.ExperimentSpec, actionTarget string) *spec.ExpModel {
	model := &spec.ExpModel{
		Target:      actionTarget,
		ActionName:  experiment.Action,
		ActionFlags: make(map[string]string, 0),
	}
	if experiment.Matchers != nil {
		for _, flag := range experiment.Matchers {
			if flag.Value == nil || len(flag.Value) == 0 {
				continue
			}
			model.ActionFlags[flag.Name] = strings.ReplaceAll(strings.Join(flag.Value, ","), "@@##", " ")
		}
	}
	return model
}

func (dc *DestroyCommand) bindFlagsFunction() func(commandFlags map[string]func() string, cmd *cobra.Command, specFlags []spec.ExpFlagSpec) {
	return func(commandFlags map[string]func() string, cmd *cobra.Command, specFlags []spec.ExpFlagSpec) {
		// set action flags
		for _, flag := range specFlags {
			flagName := flag.FlagName()
			flagDesc := flag.FlagDesc()
			if flag.FlagRequiredWhenDestroyed() {
				cmd.MarkPersistentFlagRequired(flagName)
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
		}
	}
}

// actionRunEFunc returns destroying experiment with flags
func (dc *DestroyCommand) actionRunEFunc(target, scope string, _ *actionCommand, actionCommandSpec spec.ExpActionCommandSpec) func(cmd *cobra.Command, args []string) error {
	return func(cmd *cobra.Command, args []string) error {
		expModel := createExpModel(target, scope, actionCommandSpec.Name(), cmd)
		ctx := context.Background()
		log.Infof(ctx, "destroy %+v", expModel)
		// If uid exists, use uid first. If the record cannot be found, then continue to destroy using matchers
		if uid := expModel.ActionFlags["uid"]; uid != "" {
			ctx = context.WithValue(ctx, spec.Uid, uid)
			err := dc.runDestroyWithUid(ctx, cmd, []string{uid})
			if err == nil {
				return nil
			}
			resp, ok := err.(*spec.Response)
			if ok && resp.Code != spec.DataNotFound.Code {
				return resp
			}
			ctx = context.WithValue(context.Background(), spec.Uid, uid)
			log.Warnf(ctx, "%s uid not found, so using matchers to continue to destroy", uid)
		}
		if dc.forceRemove {
			log.Warnf(ctx, "the force-remove flag does not work if the uid does not exist.")
		}
		executor := actionCommandSpec.Executor()
		executor.SetChannel(channel.NewLocalChannel())
		ctx = spec.SetDestroyFlag(ctx, spec.UnknownUid)
		response := executor.Exec(spec.UnknownUid, ctx, expModel)
		if !response.Success {
			return response
		}
		command := expModel.Target
		subCommand := expModel.ActionName
		if expModel.Scope != "" && expModel.Scope != "host" {
			command = expModel.Scope
			subCommand = fmt.Sprintf("%s %s", expModel.Target, expModel.ActionName)
		}
		// update status by finding related records
		log.Infof(ctx, "destroy by model: %+v, command: %s, subCommand: %s", expModel, command, subCommand)
		experimentModels, err := GetDS().QueryExperimentModelsByCommand(command, subCommand, expModel.ActionFlags)
		if err != nil {
			log.Warnf(ctx, "destroy success but query records failed, %v", err)
		} else {
			for _, record := range experimentModels {
				checkError(GetDS().UpdateExperimentModelByUid(record.Uid, Destroyed, ""))
			}
		}
		cmd.Println(spec.ReturnSuccess(expModel).Print())
		return nil
	}
}

func (dc *DestroyCommand) actionPostRunEFunc(actionCommand *actionCommand) func(cmd *cobra.Command, args []string) error {
	return nil
}

func destroyExample() string {
	return `
# Destroy experiment
blade destroy 47cc0744f1bb

# Force delete kubernetes experiment
blade destroy 47cc0744f1bb --target k8s --kubeconfig ~/.kube/config --force-remove`
}
