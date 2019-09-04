package exec

import (
	"context"
	"fmt"
	"github.com/chaosblade-io/chaosblade/transport"
	"testing"
)

type MockLocalChannel struct {
	Response         *transport.Response
	ScriptPath       string
	ExpectedCommands []string
	InvokeTime       int
	NoCheck          bool
	T                *testing.T
}

func (mlc *MockLocalChannel) Run(ctx context.Context, script, args string) *transport.Response {
	cmd := fmt.Sprintf("%s %s", script, args)
	if !mlc.NoCheck && mlc.ExpectedCommands[mlc.InvokeTime] != cmd {
		mlc.T.Errorf("unexpected command: %s, expected command: %s", cmd, mlc.ExpectedCommands[mlc.InvokeTime])
	}
	mlc.InvokeTime++
	return mlc.Response
}

func (mlc *MockLocalChannel) GetScriptPath() string {
	return mlc.ScriptPath
}
