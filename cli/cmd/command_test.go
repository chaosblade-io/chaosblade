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
	"strings"
	"testing"

	"github.com/spf13/cobra"

	"github.com/chaosblade-io/chaosblade/data"
)

func Test_baseCommand_recordExpModel(t *testing.T) {
	source := &MockSource{}
	SetDS(source)
	bc := &baseCommand{}

	type input struct {
		commandPath string
		command     *cobra.Command
		target      string
		scope       string
		action      string
	}
	type expect struct {
		model *data.ExperimentModel
		err   bool
	}
	delayCommand := &cobra.Command{Use: "delay"}
	var inf string
	delayCommand.PersistentFlags().StringVar(&inf, "interface", "eth0", "")
	delayCommand.PersistentFlags().SortFlags = true
	delayCommand.ParseFlags([]string{})

	tests := []struct {
		input  input
		expect expect
	}{
		{
			input{"blade create docker network delay", delayCommand, "docker", "docker", "delay"},
			expect{&data.ExperimentModel{
				Command:    "docker",
				SubCommand: "network delay",
				Flag:       " --interface=eth0",
				Status:     Created,
			}, false},
		},
		{
			input{"blade create network delay", delayCommand, "network", "", "delay"},
			expect{&data.ExperimentModel{
				Command:    "network",
				SubCommand: "delay",
				Flag:       " --interface=eth0",
				Status:     Created,
			}, false},
		},
	}
	for _, tt := range tests {
		got, err := bc.recordExpModel(tt.input.commandPath,
			createExpModel(tt.input.target, tt.input.scope, tt.input.action, tt.input.command))
		if (err != nil) != tt.expect.err {
			t.Errorf("unexpected result: %t, expected: %t", err != nil, tt.expect.err)
		}
		validateExperimentModel(got, tt.expect.model, t)
	}

}

func validateExperimentModel(result *data.ExperimentModel, expect *data.ExperimentModel, t *testing.T) {
	if result.Command != expect.Command {
		t.Errorf("unexpected command result: %v, expected: %v", result.Command, expect.Command)
	}
	if result.SubCommand != expect.SubCommand {
		t.Errorf("unexpected subcommand result: %v, expected: %v", result.SubCommand, expect.SubCommand)
	}
	if !compareFlags(result.Flag, expect.Flag) {
		t.Errorf("unexpected flag result: %v, expected: %v", result.Flag, expect.Flag)
	}
	if result.Status != expect.Status {
		t.Errorf("unexpected status result: %v, expected: %v", result.Status, expect.Status)
	}
}

func compareFlags(actual, expect string) bool {
	actualFlags := covertFlagToMap(actual)
	expectFlags := covertFlagToMap(expect)

	if len(actualFlags) != len(expectFlags) {
		return false
	}
	for key := range actualFlags {
		if _, ok := expectFlags[key]; !ok {
			return false
		}
	}
	return true
}

func covertFlagToMap(flagStr string) map[string]string {
	flags := strings.Split(flagStr, " ")
	result := make(map[string]string, 0)
	for _, flag := range flags {
		f := strings.TrimSpace(flag)
		result[f] = ""
	}
	return result
}

func Test_parseCommandPath(t *testing.T) {
	type expect struct {
		command    string
		subCommand string
		err        bool
	}
	tests := []struct {
		input  string
		expect expect
	}{
		{"blade create docker cpu fl", expect{
			command:    "docker",
			subCommand: "cpu fl",
			err:        false,
		}},
		{"blade create cpu fl", expect{
			command:    "cpu",
			subCommand: "fl",
			err:        false,
		}},
		{"blade create cpu", expect{
			command:    "",
			subCommand: "",
			err:        true,
		}},
	}
	for _, tt := range tests {
		cmd, subCmd, err := parseCommandPath(tt.input)
		if cmd != tt.expect.command {
			t.Errorf("unexpected result: %v, expected: %v", cmd, tt.expect.command)
		}
		if subCmd != tt.expect.subCommand {
			t.Errorf("unexpected result: %v, expected: %v", subCmd, tt.expect.subCommand)
		}
		if (err != nil) != tt.expect.err {
			t.Errorf("unexpected result: %t, expected: %t", err != nil, tt.expect.err)
		}
	}
}

type MockSource struct {
}

func (*MockSource) CheckAndInitExperimentTable() {
}

func (*MockSource) ExperimentTableExists() (bool, error) {
	return true, nil
}

func (*MockSource) InitExperimentTable() error {
	return nil
}

func (*MockSource) InsertExperimentModel(model *data.ExperimentModel) error {
	return nil
}

func (*MockSource) UpdateExperimentModelByUid(uid, status, errMsg string) error {
	return nil
}

// return nil for generating a new uid
func (*MockSource) QueryExperimentModelByUid(uid string) (*data.ExperimentModel, error) {
	return nil, nil
}

func (*MockSource) QueryExperimentModels(target, action, flag, status, limit string, asc bool) ([]*data.ExperimentModel, error) {
	return make([]*data.ExperimentModel, 0), nil
}

func (*MockSource) CheckAndInitPreTable() {
}

func (*MockSource) InitPreparationTable() error {
	return nil
}

func (*MockSource) PreparationTableExists() (bool, error) {
	return true, nil
}

func (*MockSource) InsertPreparationRecord(record *data.PreparationRecord) error {
	return nil
}

func (*MockSource) QueryPreparationByUid(uid string) (*data.PreparationRecord, error) {
	return &data.PreparationRecord{}, nil
}

func (*MockSource) QueryRunningPreByTypeAndProcess(programType string, processName string,
	processId string) (*data.PreparationRecord, error) {
	return &data.PreparationRecord{}, nil
}

func (*MockSource) UpdatePreparationRecordByUid(uid, status, errMsg string) error {
	return nil
}

func (*MockSource) UpdatePreparationPortByUid(uid, port string) error {
	return nil
}

func (*MockSource) UpdatePreparationPidByUid(uid, pid string) error {
	return nil
}

func (*MockSource) QueryPreparationRecords(target, status, action, flag, limit string, asc bool) ([]*data.PreparationRecord, error) {
	return make([]*data.PreparationRecord, 0), nil
}

func (*MockSource) QueryExperimentModelsByCommand(command, subCommand string, flags map[string]string) ([]*data.ExperimentModel, error) {
	return make([]*data.ExperimentModel, 0), nil
}

func (*MockSource) DeleteExperimentModelByUid(uid string) error {
	return nil
}
