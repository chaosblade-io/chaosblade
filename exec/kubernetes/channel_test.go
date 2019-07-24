package kubernetes

import (
	"context"
	"testing"

	"github.com/chaosblade-io/chaosblade/exec"
	"github.com/chaosblade-io/chaosblade/transport"
)

func TestChannel_Run(t *testing.T) {
	var ctx context.Context
	type args struct {
		namespace  string
		deployment string
		podname    string
		script     string
		args       string
	}
	tests := []struct {
		name    string
		args    args
		wantErr bool
	}{
		{"run", args{"", "", "", "burnio.sh", ""}, true},
	}
	channel := &Channel{
		channel: exec.NewLocalChannel(),
	}
	for _, tt := range tests {
		ctx = context.Background()
		ctx = context.WithValue(ctx, "namespace", tt.args.namespace)
		ctx = context.WithValue(ctx, "deployment", tt.args.deployment)
		ctx = context.WithValue(ctx, "podname", tt.args.podname)
		t.Run(tt.name, func(t *testing.T) {
			response := channel.Run(ctx, tt.args.script, tt.args.args)
			if !response.Success != tt.wantErr {
				t.Errorf("unexpected result: %t, expected result: %t", !response.Success, tt.wantErr)
			}
		})
	}
}

func TestChannel_GetBladePodByContainer_NotFound(t *testing.T) {
	channel := &Channel{
		channel: &exec.MockLocalChannel{
			Response: transport.ReturnSuccess(""),
			NoCheck:  true,
			T:        t,
		},
	}
	pod, err := channel.GetBladePodByContainer("5b282c9624", "", "weave", "")
	if pod != "" || err == nil {
		t.Error("unexpected result: found, expected result: not found")
	}
}
