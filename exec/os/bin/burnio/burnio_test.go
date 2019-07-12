package main

import (
	"testing"
)

func TestGetFileSystem(t *testing.T) {
	t.Skip("")
	tests := []struct {
		input  string
		expect bool
	}{
		{"/dev", true},
		{"devfs", false},
	}
	for _, tt := range tests {
		fs, err := getFileSystem(tt.input)
		if got := (err == nil && fs != ""); got != tt.expect {
			t.Errorf("unexpected result: %t, expected: %t", got, tt.expect)
		} 
	}
}