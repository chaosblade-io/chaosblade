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
	"reflect"
	"testing"

	"github.com/chaosblade-io/chaosblade-spec-go/spec"
)

func Test_convertCommandModel(t *testing.T) {
	type input struct {
		action, target, rules string
	}
	type expect struct {
		*spec.ExpModel
	}
	tests := []struct {
		input  input
		expect expect
	}{
		{
			input{"network delay", "docker", "--time=3000 --interface=eth0"},
			expect{&spec.ExpModel{
				Target:      "docker",
				ActionName:  "network delay",
				ActionFlags: map[string]string{"time": "3000", "interface": "eth0"},
			}},
		},
		{
			input{"delay", "network", "--time=3000 --interface=eth0"},
			expect{&spec.ExpModel{
				Target:      "network",
				ActionName:  "delay",
				ActionFlags: map[string]string{"time": "3000", "interface": "eth0"},
			}},
		},
	}
	for _, tt := range tests {
		got := spec.ConvertCommandsToExpModel(tt.input.action, tt.input.target, tt.input.rules)
		if got.Target != tt.expect.Target {
			t.Errorf("unexpected target result: %v, expected: %v", got.Target, tt.expect.Target)
		}
		if got.ActionName != tt.expect.ActionName {
			t.Errorf("unexpected action result: %v, expected: %v", got.ActionName, tt.expect.ActionName)
		}
		if !reflect.DeepEqual(got.ActionFlags, tt.expect.ActionFlags) {
			t.Errorf("unexpected flag result: %v, expected: %v", got.ActionFlags, tt.expect.ActionFlags)
		}
	}
}
