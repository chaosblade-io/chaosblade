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
			input{"network delay", "docker", "--time 3000 --interface eth0"},
			expect{&spec.ExpModel{
				Target:      "docker",
				ActionName:  "network delay",
				ActionFlags: map[string]string{"time": "3000", "interface": "eth0"},
			}},
		},
		{
			input{"delay", "network", "--time 3000 --interface eth0"},
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
			t.Errorf("unexpected result: %v, expected: %v", got.Target, tt.expect.Target)
		}
		if got.ActionName != tt.expect.ActionName {
			t.Errorf("unexpected result: %v, expected: %v", got.ActionName, tt.expect.ActionName)
		}
		if !reflect.DeepEqual(got.ActionFlags, tt.expect.ActionFlags) {
			t.Errorf("unexpected result: %v, expected: %v", got.ActionFlags, tt.expect.ActionFlags)
		}
	}
}
