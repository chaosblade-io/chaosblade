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
