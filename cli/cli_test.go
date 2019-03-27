package main

import (
	"testing"
	"bytes"
)

func TestCli_Run(t *testing.T) {
	cli := NewCli()
	cli.rootCmd.SetOutput(&bytes.Buffer{})

	err := cli.Run()
	if err != nil {
		t.Errorf("unexpected error: %v", err)
	}

	flag := cli.rootCmd.Flags().Lookup("debug")
	if flag == nil {
		t.Errorf("unexpected error: %s", "no such flag --debug")
	}

	flag = cli.rootCmd.Flags().ShorthandLookup("d")
	if flag == nil {
		t.Errorf("unexpected error: %s", "no such shorthand flag -d")
	}
}
