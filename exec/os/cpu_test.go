package os

import (
	"testing"
)

func TestParseCpuList(t *testing.T) {
	tests := []struct {
		input string
		expect []string
	}{
		{"0-3", []string{"0","1","2","3"}},
		{"1,3,5", []string{"1","3","5"}},
		{"0-2,4,6-7", []string{"0","1","2","4","6","7"}},
	}
	for _, tt := range tests {
		got, err := parseCpuList(tt.input)
		if err != nil {
			t.Errorf("input is illegal")
		}
		if len(got) != len(tt.expect) {
			t.Errorf("expected to see %d cpu, got %d", len(tt.expect), len(got))
		}
		for i, m := range tt.expect {
			if got[i] != m {
				t.Errorf("unexpected result: %s, expected: %s", got, tt.expect)
			}
		}
	}
}