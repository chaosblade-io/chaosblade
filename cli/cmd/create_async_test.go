/*
 * Copyright 2025 The ChaosBlade Authors
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 */

package cmd

import (
	"bytes"
	"context"
	"testing"

	"github.com/spf13/cobra"

	"github.com/chaosblade-io/chaosblade-spec-go/spec"

	"github.com/chaosblade-io/chaosblade/data"
)

// asyncRecordMockSource returns the experiment record for the uid, mirroring
// the state where `blade create --async` has inserted the record before
// re-executing the very same command with --nohup=true.
type asyncRecordMockSource struct {
	*MockSource
	updatedStatus string
}

func (m *asyncRecordMockSource) QueryExperimentModelByUid(uid string) (*data.ExperimentModel, error) {
	return &data.ExperimentModel{
		Uid:        uid,
		Command:    "cpu",
		SubCommand: "load",
		Status:     Created,
	}, nil
}

func (m *asyncRecordMockSource) UpdateExperimentModelByUid(uid, status, errMsg string) error {
	m.updatedStatus = status
	return nil
}

type fakeActionExecutor struct{}

func (*fakeActionExecutor) Name() string { return "fake" }

func (*fakeActionExecutor) SetChannel(channel spec.Channel) {}

func (*fakeActionExecutor) Exec(uid string, ctx context.Context, model *spec.ExpModel) *spec.Response {
	return spec.ReturnSuccess(uid)
}

type fakeActionCommandSpec struct {
	spec.BaseExpActionCommandSpec
}

func (*fakeActionCommandSpec) Name() string { return "load" }

func (*fakeActionCommandSpec) Aliases() []string { return nil }

func (*fakeActionCommandSpec) ShortDesc() string { return "fake load" }

func (*fakeActionCommandSpec) LongDesc() string { return "fake load" }

// TestCreateNohupReExecution drives the exact command line that the async
// create path re-executes in background:
//
//	blade create cpu load --uid <uid> --nohup=true
//
// On the nohup path no new record is created, so resp stays nil; reading
// resp.Result must not crash the blade process with a nil pointer panic.
func TestCreateNohupReExecution(t *testing.T) {
	mock := &asyncRecordMockSource{MockSource: &MockSource{}}
	SetDS(mock)

	cc := &CreateCommand{}
	ac := &actionCommand{
		ExpActionFlags: &ExpActionFlags{
			ActionFlags:  make(map[string]func() string),
			MatcherFlags: make(map[string]func() string),
		},
	}
	actionSpec := &fakeActionCommandSpec{
		BaseExpActionCommandSpec: spec.BaseExpActionCommandSpec{
			ActionExecutor: &fakeActionExecutor{},
		},
	}

	out := &bytes.Buffer{}
	root := &cobra.Command{Use: "blade", SilenceErrors: true, SilenceUsage: true}
	root.SetOut(out)
	root.SetErr(out)

	createCmd := &cobra.Command{Use: "create"}
	// Same persistent flags as registered by CreateCommand.Init
	createCmd.PersistentFlags().String(UidFlag, "", "uid")
	createCmd.PersistentFlags().Bool(AsyncFlag, false, "async")
	createCmd.PersistentFlags().String(EndpointFlag, "", "endpoint")
	createCmd.PersistentFlags().Bool(NohupFlag, false, "nohup")

	cpuCmd := &cobra.Command{Use: "cpu"}
	loadCmd := &cobra.Command{
		Use: "load",
		RunE: func(cmd *cobra.Command, args []string) error {
			return cc.actionRunEFunc("cpu", "host", ac, actionSpec)(cmd, args)
		},
	}
	cpuCmd.AddCommand(loadCmd)
	createCmd.AddCommand(cpuCmd)
	root.AddCommand(createCmd)
	root.SetArgs([]string{"create", "cpu", "load", "--uid", "async-uid-1", "--nohup=true"})

	var err error
	func() {
		defer func() {
			if r := recover(); r != nil {
				t.Errorf("re-executing `blade create` with --nohup=true panicked: %v", r)
			}
		}()
		err = root.Execute()
	}()

	if t.Failed() {
		return
	}
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if mock.updatedStatus != Success {
		t.Fatalf("expected experiment record updated to %q, got %q", Success, mock.updatedStatus)
	}
}
