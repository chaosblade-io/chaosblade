package util

import "testing"

func TestIsExist_ForMountPoint(t *testing.T) {
	tests := []struct {
		device string
		want   bool
	}{
		{"/", true},
		{"/dev", true},
		{"devfs", false},
	}
	for _, tt := range tests {
		if got := IsExist(tt.device); got != tt.want {
			t.Errorf("unexpected result: %t, expected: %t", got, tt.want)
		}
	}
}
