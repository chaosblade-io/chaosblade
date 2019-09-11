package cmd

import (
	"testing"

	"github.com/chaosblade-io/chaosblade/data"
)

func Test_baseCommand_recordExpModel(t *testing.T) {
	source := &MockSource{}
	SetDS(source)
	bc := &baseCommand{}

	type input struct {
		commandPath, flag string
	}
	type expect struct {
		model *data.ExperimentModel
		err   bool
	}
	tests := []struct {
		input  input
		expect expect
	}{
		{
			input{"blade create docker network delay", "--time 3000 --interface eth0"},
			expect{&data.ExperimentModel{
				Command:    "docker",
				SubCommand: "network delay",
				Flag:       "--time 3000 --interface eth0",
				Status:     "Created",
			}, false},
		},
		{
			input{"blade create network delay", "--time 3000 --interface eth0"},
			expect{&data.ExperimentModel{
				Command:    "network",
				SubCommand: "delay",
				Flag:       "--time 3000 --interface eth0",
				Status:     "Created",
			}, false},
		},
	}
	for _, tt := range tests {
		got, err := bc.recordExpModel(tt.input.commandPath, tt.input.flag)
		if (err != nil) != tt.expect.err {
			t.Errorf("unexpected result: %t, expected: %t", err != nil, tt.expect.err)
		}
		validateExperimentModel(got, tt.expect.model, t)
	}

}

func validateExperimentModel(result *data.ExperimentModel, expect *data.ExperimentModel, t *testing.T) {
	if result.Command != expect.Command {
		t.Errorf("unexpected result: %v, expected: %v", result.Command, expect.Command)
	}
	if result.SubCommand != expect.SubCommand {
		t.Errorf("unexpected result: %v, expected: %v", result.SubCommand, expect.SubCommand)
	}
	if result.Flag != expect.Flag {
		t.Errorf("unexpected result: %v, expected: %v", result.Flag, expect.Flag)
	}
	if result.Status != expect.Status {
		t.Errorf("unexpected result: %v, expected: %v", result.Status, expect.Status)
	}
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

func (*MockSource) ListExperimentModels() ([]*data.ExperimentModel, error) {
	return make([]*data.ExperimentModel, 0), nil
}

func (*MockSource) QueryExperimentModelsByCommand(target string) ([]*data.ExperimentModel, error) {
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

func (*MockSource) ListPreparationRecords() ([]*data.PreparationRecord, error) {
	return make([]*data.PreparationRecord, 0), nil
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
