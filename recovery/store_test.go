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

package recovery

import (
	"context"
	"errors"
	"fmt"
	"sync"
	"testing"
	"time"
)

type fakeBackend struct {
	mu                                sync.Mutex
	effect                            string
	applies, recovers                 int
	applyErr, recoverErr, identityErr error
	entered, proceed                  chan struct{}
}

func (f *fakeBackend) Resolve(context.Context, Target) (Identity, error) {
	return Identity{BootID: "boot", NetNS: "netns", PID: 1, Interface: "eth0", InterfaceIndex: 2}, nil
}

func (f *fakeBackend) Inspect(context.Context, Record) (string, bool, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.effect, f.effect == "", f.identityErr
}

func (f *fakeBackend) Apply(context.Context, Record) error {
	if f.entered != nil {
		close(f.entered)
		<-f.proceed
	}
	f.mu.Lock()
	defer f.mu.Unlock()
	f.applies++
	f.effect = "owned"
	return f.applyErr
}

func (f *fakeBackend) Recover(context.Context, Record) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.recovers++
	if f.recoverErr == nil {
		f.effect = ""
	}
	return f.recoverErr
}

func testPlan() Plan {
	return Plan{Version: 1, ClusterID: "cluster", CRUID: "cr", RunID: "run", Name: "test", Generation: 1, Nodes: []Node{{UID: "node-uid", Name: "node", Address: "https://127.0.0.1:9444", ServerName: "recovery.test"}}, Targets: []Target{{ID: "target", NodeUID: "node-uid", Scope: "node", Action: "delay", Flags: map[string]string{"interface": "eth0", "time": "50"}}}}
}

func openTest(t *testing.T, dir string, b Backend) *Manager {
	t.Helper()
	m, err := Open(dir, "cluster", "node", b)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { m.Close() })
	return m
}

func registerTest(t *testing.T, m *Manager, p Plan) {
	t.Helper()
	if _, err := m.Register(context.Background(), p); err != nil {
		t.Fatal(err)
	}
}

func TestRecoveryPersistsAcrossRestartAndIsIdempotent(t *testing.T) {
	ctx := context.Background()
	dir := t.TempDir()
	f := &fakeBackend{}
	m := openTest(t, dir, f)
	p := testPlan()
	registerTest(t, m, p)
	if _, err := m.Apply(ctx, p.RunID, "target"); err != nil {
		t.Fatal(err)
	}
	m.Close()
	m = openTest(t, dir, f)
	for i := 0; i < 2; i++ {
		s, err := m.Stop(ctx, p.RunID)
		if err != nil || !Complete(p, s) {
			t.Fatalf("stop: %+v %v", s, err)
		}
	}
	if _, err := m.Apply(ctx, p.RunID, "target"); err == nil {
		t.Fatal("late injection accepted")
	}
	if f.applies != 1 || f.recovers != 1 {
		t.Fatalf("unexpected side effects %d/%d", f.applies, f.recovers)
	}
	registerTest(t, m, p) // Register retries cannot clear a tombstone.
	s, _ := m.Status(ctx, p.RunID)
	if !s.StopRequested {
		t.Fatal("lost stop intent")
	}
}

func TestConcurrentStopWaitsForApplyAndFencesLateRequest(t *testing.T) {
	ctx := context.Background()
	dir := t.TempDir()
	f := &fakeBackend{entered: make(chan struct{}), proceed: make(chan struct{})}
	m := openTest(t, dir, f)
	other := openTest(t, dir, f)
	p := testPlan()
	registerTest(t, m, p)
	applied := make(chan error, 1)
	go func() { _, err := m.Apply(ctx, p.RunID, "target"); applied <- err }()
	<-f.entered
	stopped := make(chan NodeStatus, 1)
	go func() {
		s, err := other.Stop(ctx, p.RunID)
		if err != nil {
			s.Error = err.Error()
		}
		stopped <- s
	}()
	deadline := time.After(3 * time.Second)
	for {
		s, err := other.Status(ctx, p.RunID)
		if err != nil {
			t.Fatal(err)
		}
		if s.StopRequested {
			break
		}
		select {
		case <-deadline:
			t.Fatal("stop intent waited for apply lock")
		case <-time.After(time.Millisecond):
		}
	}
	close(f.proceed)
	if err := <-applied; err != nil {
		t.Fatal(err)
	}
	s := <-stopped
	if !Complete(p, s) {
		t.Fatalf("concurrent stop incomplete: %+v", s)
	}
	if _, err := m.Apply(ctx, p.RunID, "target"); err == nil {
		t.Fatal("late apply accepted")
	}
}

func TestUnknownOwnershipAndReplacementNeverDeleteRules(t *testing.T) {
	for _, kind := range []string{"changed-qdisc", "changed-identity", "crash-before-receipt", "failed-apply"} {
		t.Run(kind, func(t *testing.T) {
			ctx := context.Background()
			f := &fakeBackend{}
			m := openTest(t, t.TempDir(), f)
			p := testPlan()
			registerTest(t, m, p)
			if kind == "failed-apply" {
				f.applyErr = errors.New("executor failed after side effect")
			}
			_, _ = m.Apply(ctx, p.RunID, "target")
			switch kind {
			case "changed-qdisc":
				f.effect = "foreign"
			case "changed-identity":
				f.identityErr = errors.New("namespace inode changed")
			case "crash-before-receipt":
				j, _ := m.load(ctx, p.RunID)
				j.Records[0].Fingerprint = ""
				setState(&j.Records[0], Applying, nil)
				if err := m.save(ctx, j); err != nil {
					t.Fatal(err)
				}
			}
			s, err := m.Stop(ctx, p.RunID)
			if err != nil {
				t.Fatal(err)
			}
			if Complete(p, s) || s.Targets[0].State != Unknown || f.recovers != 0 {
				t.Fatalf("unsafe cleanup: %+v, calls=%d", s, f.recovers)
			}
		})
	}
}

func TestCleanupFailureIsRetryable(t *testing.T) {
	ctx := context.Background()
	f := &fakeBackend{recoverErr: errors.New("temporary tc failure")}
	m := openTest(t, t.TempDir(), f)
	p := testPlan()
	registerTest(t, m, p)
	_, _ = m.Apply(ctx, p.RunID, "target")
	s, err := m.Stop(ctx, p.RunID)
	if err != nil || Complete(p, s) {
		t.Fatalf("false success: %+v %v", s, err)
	}
	f.recoverErr = nil
	s, err = m.Stop(ctx, p.RunID)
	if err != nil || !Complete(p, s) {
		t.Fatalf("retry: %+v %v", s, err)
	}
}

func TestPlanAndResourceConflictsAndLegacyGuard(t *testing.T) {
	ctx := context.Background()
	f := &fakeBackend{}
	m := openTest(t, t.TempDir(), f)
	p := testPlan()
	registerTest(t, m, p)
	changed := testPlan()
	changed.Targets[0].Flags["time"] = "100"
	if _, err := m.Register(ctx, changed); err == nil {
		t.Fatal("changed plan accepted")
	}
	changed = testPlan()
	changed.RunID = "second"
	changed.CRUID = "second-cr"
	if _, err := m.Register(ctx, changed); err == nil {
		t.Fatal("conflicting interface accepted")
	}
	if _, err := m.GuardLegacy(ctx); err == nil {
		t.Fatal("legacy network command can overwrite managed effect")
	}
	if _, err := m.Stop(ctx, p.RunID); err != nil {
		t.Fatal(err)
	}
	unlock, err := m.GuardLegacy(ctx)
	if err != nil {
		t.Fatal(err)
	}
	unlock()
	registerTest(t, m, changed)
}

func TestStopBeforeInjectionAndIncompleteResponses(t *testing.T) {
	ctx := context.Background()
	f := &fakeBackend{}
	m := openTest(t, t.TempDir(), f)
	p := testPlan()
	registerTest(t, m, p)
	s, err := m.Stop(ctx, p.RunID)
	if err != nil || !Complete(p, s) {
		t.Fatal(s, err)
	}
	if f.applies != 0 || f.recovers != 0 {
		t.Fatal("unnecessary effect on an uninjected target")
	}
	s.Targets = nil
	if Complete(p, s) {
		t.Fatal("empty result considered complete")
	}
}

func TestValidationRejectsUnboundedOrUnsafeRequests(t *testing.T) {
	for i, edit := range []func(*Plan){
		func(p *Plan) { p.Version = 2 }, func(p *Plan) { p.Nodes[0].Address = "http://node" },
		func(p *Plan) { p.Targets[0].Flags["force"] = "true" }, func(p *Plan) { p.Targets[0].Flags["interface"] = "eth0;id" },
		func(p *Plan) { p.Targets[0].Flags["time"] = "0" }, func(p *Plan) { p.Targets[0].Scope = "container" },
		func(p *Plan) { p.Targets[0].Flags["destination-ip"] = "10.0.0.1" },
	} {
		t.Run(fmt.Sprint(i), func(t *testing.T) {
			p := testPlan()
			edit(&p)
			if ValidatePlan(p) == nil {
				t.Fatal("unsafe request accepted")
			}
		})
	}
}

func TestCancelBeforeRegistrationFencesLateInjection(t *testing.T) {
	ctx := context.Background()
	f := &fakeBackend{}
	m := openTest(t, t.TempDir(), f)
	p := testPlan()
	s, err := m.Cancel(ctx, p)
	if err != nil || !Complete(p, s) {
		t.Fatal(s, err)
	}
	registerTest(t, m, p)
	if _, err = m.Apply(ctx, p.RunID, "target"); err == nil {
		t.Fatal("cancelled preparation allowed a late injection")
	}
	if f.applies != 0 || f.recovers != 0 {
		t.Fatal("cancellation changed the network")
	}
}

func TestPersistentDirectoryCannotChangeClusterIdentity(t *testing.T) {
	dir := t.TempDir()
	m := openTest(t, dir, &fakeBackend{})
	m.Close()
	if other, err := Open(dir, "different-cluster", "node", &fakeBackend{}); err == nil {
		other.Close()
		t.Fatal("cross-cluster journal reuse accepted")
	}
}

func TestInterruptedInjectionNeverReportsRecoveredFromOneCleanRead(t *testing.T) {
	ctx := context.Background()
	m := openTest(t, t.TempDir(), &fakeBackend{})
	p := testPlan()
	registerTest(t, m, p)
	j, err := m.load(ctx, p.RunID)
	if err != nil {
		t.Fatal(err)
	}
	setState(&j.Records[0], Applying, nil)
	if err = m.save(ctx, j); err != nil {
		t.Fatal(err)
	}
	s, err := m.Stop(ctx, p.RunID)
	if err != nil || Complete(p, s) || s.Targets[0].State != Unknown {
		t.Fatal(s, err)
	}
}
