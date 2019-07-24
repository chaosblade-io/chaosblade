package exec

import (
	"testing"
)

func TestIsCommandAvailable(t *testing.T) {
	tests := []struct {
		input  string
		expect bool
	}{
		{"tc", true},
		{"grep", true},
		{"lsss", false},
	}
	for _, tt := range tests {
		got := IsCommandAvailable(tt.input)
		if got != tt.expect {
			t.Errorf("unexpected result: %t, expected: %t", got, tt.expect)
		}
	}
}
