package cmd

import (
	"testing"

	"github.com/chaosblade-io/chaosblade/data"
)

func TestPrepareJvmCommand_insertPrepareRecord(t *testing.T) {
	type input struct {
		prepareType string
		flags       []string
	}
	type expect struct {
		record *data.PreparationRecord
		err    bool
	}
	tests := []struct {
		input  input
		expect expect
	}{
		{
			input{
				PrepareJvmType, []string{"project.name", "8703", ""},
			},
			expect{
				&data.PreparationRecord{
					ProgramType: PrepareJvmType,
					Process:     "project.name",
					Port:        "8703",
					Status:      Created,
				}, false,
			},
		},
	}
	for _, tt := range tests {
		got, err := insertPrepareRecord(tt.input.prepareType, tt.input.flags[0], tt.input.flags[1], tt.input.flags[2])
		if (err != nil) != tt.expect.err {
			t.Errorf("unexpected result: %t, expected: %t", err != nil, tt.expect.err)
		}
		validatePreparationRecord(got, tt.expect.record, t)
	}
}

func validatePreparationRecord(result, expect *data.PreparationRecord, t *testing.T) {
	if result.ProgramType != expect.ProgramType {
		t.Errorf("unexpected result: %v, expected: %v", result.ProgramType, expect.ProgramType)
	}
	if result.Process != expect.Process {
		t.Errorf("unexpected result: %v, expected: %v", result.Process, expect.Process)
	}
	if result.Port != expect.Port {
		t.Errorf("unexpected result: %v, expected: %v", result.Port, expect.Port)
	}
	if result.Status != expect.Status {
		t.Errorf("unexpected result: %v, expected: %v", result.Status, expect.Status)
	}
}
