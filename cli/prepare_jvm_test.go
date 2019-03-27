package main

import (
	"testing"
	"bytes"
	"github.com/spf13/cobra"
)

func TestPrepareJvmCommand_Run(t *testing.T) {
	jvmCommand := &PrepareJvmCommand{}
	jvmCommand.Init()
	jvmCommand.command.SetOutput(&bytes.Buffer{})
	jvmCommand.command.RunE = func(cmd *cobra.Command, args []string) error {
		return nil
	}
	jvmCommand.command.Execute()

	flag := jvmCommand.command.Flags().Lookup("process")
	if flag == nil {
		t.Errorf("unexpected error: %s", "no such flag --process")
	}
}
