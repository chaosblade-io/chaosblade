package cmd

import (
	"testing"

	"reflect"

	"github.com/chaosblade-io/chaosblade/exec"
)

func Test_convertCommandModel(t *testing.T) {
	type input struct {
		action, target, rules string
	}
	type expect struct {
		*exec.ExpModel
	}
	tests := []struct {
		input  input
		expect expect
	}{
		{
			input{"network delay", "docker", "--time 3000 --interface eth0"},
			expect{&exec.ExpModel{
				Target:      "docker",
				ActionName:  "network delay",
				ActionFlags: map[string]string{"time": "3000", "interface": "eth0"},
			}},
		},
		{
			input{"delay", "network", "--time 3000 --interface eth0"},
			expect{&exec.ExpModel{
				Target:      "network",
				ActionName:  "delay",
				ActionFlags: map[string]string{"time": "3000", "interface": "eth0"},
			}},
		},
	}
	for _, tt := range tests {
		got := convertCommandModel(tt.input.action, tt.input.target, tt.input.rules)
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
