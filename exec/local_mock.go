package exec

import (
	"github.com/chaosblade-io/chaosblade/transport"
	"context"
	"fmt"
	"testing"
)

type MockLocalChannel struct {
	Response        *transport.Response
	ScriptPath      string
	ExpectedCommand string
	NoCheck    		bool
	T               *testing.T
}

func (mlc *MockLocalChannel) Run(ctx context.Context, script, args string) *transport.Response {
	cmd := fmt.Sprintf("%s %s", script, args)
	if !mlc.NoCheck && mlc.ExpectedCommand != cmd {
		mlc.T.Errorf("unexpected command: %s, expected command: %s", cmd, mlc.ExpectedCommand)
	}
	return mlc.Response
}

func (mlc *MockLocalChannel) GetScriptPath() string {
	return mlc.ScriptPath
}