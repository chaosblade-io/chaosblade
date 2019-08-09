package exec

import (
	"context"
	"github.com/chaosblade-io/chaosblade/transport"
)

type Channel interface {
	// Run command
	Run(ctx context.Context, script, args string) *transport.Response

	// GetScriptPath
	GetScriptPath() string
}
