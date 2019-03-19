package exec

import (
	"context"
	"github.com/chaosblade-io/chaosblade/transport"
	"fmt"
	"strings"
)

const (
	DestroyKey = "suid"
)

// ExpModel is the experiment data object
type ExpModel struct {
	// Target is experiment target
	Target string

	// ActionName is the experiment action FlagName, for example delay
	ActionName string

	// ActionFlags is the experiment action flags, for example time and offset
	ActionFlags map[string]string
}

// Executor defines the executor interface
type Executor interface {
	// FlagName is used to identify the executor
	Name() string

	// Exec is used to execute the experiment
	Exec(uid string, ctx context.Context, model *ExpModel) *transport.Response

	SetChannel(channel Channel)
}

type PreExecutor interface {
	PreExec(cmdName, parentCmdName string, flags map[string]string) func(ctx context.Context) (Channel, context.Context, error)
}

func (exp *ExpModel) GetFlags() string {
	flags := make([]string, 0)
	for k, v := range exp.ActionFlags {
		if v == "" {
			continue
		}
		flags = append(flags, fmt.Sprintf("--%s %s", k, v))
	}
	return strings.Join(flags, " ")
}

func SetDestroyFlag(ctx context.Context, suid string) context.Context {
	return context.WithValue(ctx, DestroyKey, suid)
}

// IsDestroy command
func IsDestroy(ctx context.Context) (string, bool) {
	suid := ctx.Value(DestroyKey)
	if suid == nil {
		return "", false
	}
	return suid.(string), true
}
