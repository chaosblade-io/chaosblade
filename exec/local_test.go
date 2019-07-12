package exec

import (
	"testing"
	"context"
)


func TestGetPidsByProcessName(t *testing.T) {
	tests := []struct {
		input string
		expect string
	}{
		{"init", "1"},
		{"kthreadd", "2"},
	}
	for _, tt := range tests {
		got, err := GetPidsByProcessName(tt.input, context.Background())
		if err != nil  || len(got) == 0 {
			t.Errorf("no process called %s is running", tt.input)
		}
		if got[0] != tt.expect {
			t.Errorf("unexpected result: %s, expected: %s", got[0], tt.expect)
		}
	}
}

func TestIsCommandAvailable(t *testing.T) {
	tests := []struct {
		input string
		expect bool
	}{
		{"cd", true},
		{"ls", true},
		{"lsss", false},
	}
	for _, tt := range tests {
		got := IsCommandAvailable(tt.input)
		if got != tt.expect {
			t.Errorf("unexpected result: %t, expected: %t", got, tt.expect)
		}
	}
}