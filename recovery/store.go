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
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"time"

	_ "github.com/glebarez/go-sqlite"

	api "github.com/chaosblade-io/chaosblade/apis/chaosblade/v1alpha1"
)

// Manager serializes local effects across CLI and service processes. The journal
// lives in the same persistent directory as its lock, including SQLite sidecars.
type Manager struct {
	db        *sql.DB
	dir       string
	clusterID string
	nodeName  string
	backend   Backend
}

type journal struct {
	Plan    Plan     `json:"plan"`
	NodeUID string   `json:"nodeUID"`
	Records []Record `json:"records"`
	Stop    bool     `json:"-"`
}

// Open requires an explicit persistent directory; it never falls back to /tmp.
func Open(dir, clusterID, nodeName string, backend Backend) (*Manager, error) {
	if !filepath.IsAbs(dir) || !identifier.MatchString(clusterID) || nodeName == "" {
		return nil, fmt.Errorf("absolute state directory, cluster ID and node name are required")
	}
	if err := os.MkdirAll(dir, 0o700); err != nil {
		return nil, err
	}
	file := filepath.Join(dir, "recovery.db")
	f, err := os.OpenFile(file, os.O_CREATE|os.O_RDWR, 0o600)
	if err != nil {
		return nil, err
	}
	f.Close()
	db, err := sql.Open("sqlite", file)
	if err != nil {
		return nil, err
	}
	db.SetMaxOpenConns(1)
	m := &Manager{db: db, dir: dir, clusterID: clusterID, nodeName: nodeName, backend: backend}
	_, err = db.Exec(`PRAGMA busy_timeout=5000;
 PRAGMA synchronous=FULL;
 CREATE TABLE IF NOT EXISTS recovery_identity_v1 (id INTEGER PRIMARY KEY CHECK(id=1), cluster_id TEXT NOT NULL, node_name TEXT NOT NULL);
 CREATE TABLE IF NOT EXISTS recovery_runs_v1 (id TEXT PRIMARY KEY, digest TEXT NOT NULL, stop INTEGER NOT NULL DEFAULT 0, journal TEXT NOT NULL);
 CREATE TABLE IF NOT EXISTS recovery_claims_v1 (resource TEXT PRIMARY KEY, run_id TEXT NOT NULL, target_id TEXT NOT NULL);`)
	if err != nil {
		db.Close()
		return nil, err
	}
	_, err = db.Exec("INSERT OR IGNORE INTO recovery_identity_v1(id,cluster_id,node_name) VALUES(1,?,?)", clusterID, nodeName)
	if err != nil {
		db.Close()
		return nil, err
	}
	var savedCluster, savedNode string
	if err = db.QueryRow("SELECT cluster_id,node_name FROM recovery_identity_v1 WHERE id=1").Scan(&savedCluster, &savedNode); err != nil {
		db.Close()
		return nil, err
	}
	if savedCluster != clusterID || savedNode != nodeName {
		db.Close()
		return nil, fmt.Errorf("persistent recovery directory belongs to a different cluster or node")
	}
	return m, nil
}

func (m *Manager) Close() error { return m.db.Close() }

func (m *Manager) load(ctx context.Context, id string) (journal, error) {
	var j journal
	var data string
	var stop bool
	err := m.db.QueryRowContext(ctx, "SELECT journal, stop FROM recovery_runs_v1 WHERE id = ?", id).Scan(&data, &stop)
	if err != nil {
		return j, err
	}
	err = json.Unmarshal([]byte(data), &j)
	if err == nil && (j.Plan.ClusterID != m.clusterID || localNode(j.Plan, m.nodeName) != j.NodeUID) {
		return j, fmt.Errorf("journal identity does not match this node")
	}
	j.Stop = stop
	return j, err
}

func localNode(plan Plan, name string) string {
	for _, n := range plan.Nodes {
		if n.Name == name {
			return n.UID
		}
	}
	return ""
}

func (m *Manager) save(ctx context.Context, j journal) error {
	b, err := json.Marshal(j)
	if err != nil {
		return err
	}
	_, err = m.db.ExecContext(ctx, "UPDATE recovery_runs_v1 SET journal = ? WHERE id = ?", string(b), j.Plan.RunID)
	return err
}

func resourceKey(id Identity) string {
	return fmt.Sprintf("%s/%s/%d", id.BootID, id.NetNS, id.InterfaceIndex)
}

// Register persists the complete immutable plan and claims local resources before
// any effect. Retried registration never resolves a replacement pod or container.
func (m *Manager) Register(ctx context.Context, plan Plan) (NodeStatus, error) {
	return m.register(ctx, plan, false)
}

// Cancel records a terminal tombstone even when preparation failed before a
// local target could be resolved. This closes the late-registration race.
func (m *Manager) Cancel(ctx context.Context, plan Plan) (NodeStatus, error) {
	if _, err := m.register(ctx, plan, true); err != nil {
		return NodeStatus{}, err
	}
	return m.Stop(ctx, plan.RunID)
}

func (m *Manager) register(ctx context.Context, plan Plan, stopped bool) (NodeStatus, error) {
	if err := ValidatePlan(plan); err != nil {
		return NodeStatus{}, err
	}
	if plan.ClusterID != m.clusterID || localNode(plan, m.nodeName) == "" {
		return NodeStatus{}, fmt.Errorf("plan does not belong to this cluster and node")
	}
	unlock, err := m.lock(ctx)
	if err != nil {
		return NodeStatus{}, err
	}
	defer unlock()
	old, err := m.load(ctx, plan.RunID)
	if err == nil {
		if Digest(old.Plan) != Digest(plan) {
			return NodeStatus{}, fmt.Errorf("run ID already has a different plan")
		}
		return status(old), nil
	}
	if !errors.Is(err, sql.ErrNoRows) {
		return NodeStatus{}, err
	}
	var count int
	if err = m.db.QueryRowContext(ctx, "SELECT count(*) FROM recovery_runs_v1").Scan(&count); err != nil {
		return NodeStatus{}, err
	}
	if count >= 1024 {
		return NodeStatus{}, fmt.Errorf("recovery journal capacity reached (1024 runs); refusing new injection")
	}
	j := journal{Plan: plan, NodeUID: localNode(plan, m.nodeName), Stop: stopped}
	seen := map[string]bool{}
	for _, t := range plan.Targets {
		if t.NodeUID != j.NodeUID {
			continue
		}
		if stopped {
			r := Record{Target: t}
			setState(&r, Recovered, nil)
			j.Records = append(j.Records, r)
			continue
		}
		id, err := m.backend.Resolve(ctx, t)
		if err != nil {
			return NodeStatus{}, fmt.Errorf("resolve %s: %w", t.ID, err)
		}
		if id.BootID == "" || id.NetNS == "" || id.InterfaceIndex < 1 {
			return NodeStatus{}, fmt.Errorf("incomplete target identity")
		}
		key := resourceKey(id)
		if seen[key] {
			return NodeStatus{}, fmt.Errorf("multiple targets share a network interface")
		}
		seen[key] = true
		r := Record{Target: t, Identity: id}
		_, clean, inspectErr := m.backend.Inspect(ctx, r)
		if inspectErr != nil {
			return NodeStatus{}, fmt.Errorf("inspect %s: %w", t.ID, inspectErr)
		}
		if !clean {
			return NodeStatus{}, fmt.Errorf("target %s already has a qdisc; refusing registration", t.ID)
		}
		setState(&r, Registered, nil)
		j.Records = append(j.Records, r)
	}
	if len(j.Records) == 0 {
		return NodeStatus{}, fmt.Errorf("plan contains no targets for this node")
	}
	b, err := json.Marshal(j)
	if err != nil {
		return NodeStatus{}, err
	}
	tx, err := m.db.BeginTx(ctx, nil)
	if err != nil {
		return NodeStatus{}, err
	}
	defer tx.Rollback()
	_, err = tx.ExecContext(ctx, "INSERT INTO recovery_runs_v1(id,digest,journal,stop) VALUES(?,?,?,?)", plan.RunID, Digest(plan), string(b), stopped)
	if err != nil {
		return NodeStatus{}, err
	}
	for _, r := range j.Records {
		if stopped {
			continue
		}
		_, err = tx.ExecContext(ctx, "INSERT INTO recovery_claims_v1(resource,run_id,target_id) VALUES(?,?,?)", resourceKey(r.Identity), plan.RunID, r.Target.ID)
		if err != nil {
			return NodeStatus{}, fmt.Errorf("interface is already claimed: %w", err)
		}
	}
	if err = tx.Commit(); err != nil {
		return NodeStatus{}, err
	}
	return status(j), nil
}

func setState(r *Record, state string, err error) {
	r.Status = TargetStatus{ID: r.Target.ID, State: state, UpdatedAt: time.Now().UTC().Format(time.RFC3339Nano)}
	if err != nil {
		r.Status.Error = err.Error()
	}
}

func status(j journal) NodeStatus {
	s := NodeStatus{NodeUID: j.NodeUID, RunID: j.Plan.RunID, PlanDigest: Digest(j.Plan), StopRequested: j.Stop, Targets: []TargetStatus{}}
	for _, r := range j.Records {
		s.Targets = append(s.Targets, r.Status)
	}
	return s
}

func (m *Manager) Status(ctx context.Context, id string) (NodeStatus, error) {
	j, err := m.load(ctx, id)
	if err != nil {
		return NodeStatus{}, err
	}
	active := false
	for _, r := range j.Records {
		active = active || r.Status.State == Active
	}
	if !active {
		return status(j), nil
	}
	unlock, err := m.lock(ctx)
	if err != nil {
		return NodeStatus{}, err
	}
	defer unlock()
	j, err = m.load(ctx, id)
	if err != nil {
		return NodeStatus{}, err
	}
	changed := false
	for i := range j.Records {
		r := &j.Records[i]
		if r.Status.State != Active {
			continue
		}
		fp, clean, e := m.backend.Inspect(ctx, *r)
		if e == nil && (clean || fp != r.Fingerprint) {
			e = fmt.Errorf("recorded effect no longer matches the network interface")
		}
		if e != nil {
			setState(r, Unknown, e)
			changed = true
		}
	}
	if changed {
		if err = m.save(ctx, j); err != nil {
			return NodeStatus{}, err
		}
	}
	return status(j), nil
}

func (m *Manager) Plan(ctx context.Context, id string) (Plan, error) {
	j, err := m.load(ctx, id)
	return j.Plan, err
}

// Apply accepts only a previously registered target. A stop tombstone is terminal
// for this run, including when an older Operator request arrives after recovery.
func (m *Manager) Apply(ctx context.Context, id, targetID string) (NodeStatus, error) {
	unlock, err := m.lock(ctx)
	if err != nil {
		return NodeStatus{}, err
	}
	defer unlock()
	j, err := m.load(ctx, id)
	if err != nil {
		return NodeStatus{}, err
	}
	if j.Stop {
		return status(j), fmt.Errorf("run is stopped")
	}
	index := -1
	for i, r := range j.Records {
		if r.Target.ID == targetID {
			index = i
			break
		}
	}
	if index < 0 {
		return status(j), fmt.Errorf("target is not registered on this node")
	}
	r := &j.Records[index]
	if r.Status.State == Active {
		return status(j), nil
	}
	if r.Status.State != Registered {
		return status(j), fmt.Errorf("target state %s does not permit injection", r.Status.State)
	}
	_, clean, err := m.backend.Inspect(ctx, *r)
	if err != nil || !clean {
		if err == nil {
			err = fmt.Errorf("interface has an existing qdisc; refusing to replace it")
		}
		return status(j), err
	}
	setState(r, Applying, nil)
	if err = m.save(ctx, j); err != nil {
		return status(j), err
	}
	// The stop write does not wait for the effect lock. Recheck it immediately
	// before applying; a concurrent stop after this point waits and cleans up.
	var stopped bool
	if err = m.db.QueryRowContext(ctx, "SELECT stop FROM recovery_runs_v1 WHERE id = ?", id).Scan(&stopped); err != nil {
		return status(j), err
	}
	if stopped {
		setState(r, Registered, nil)
		if err = m.save(ctx, j); err != nil {
			return status(j), err
		}
		return status(j), fmt.Errorf("run is stopped")
	}
	applyErr := m.backend.Apply(ctx, *r)
	fp, clean, inspectErr := m.backend.Inspect(ctx, *r)
	if applyErr == nil && inspectErr == nil && !clean && fp != "" {
		r.Fingerprint = fp
		setState(r, Active, nil)
	} else {
		err = errors.Join(applyErr, inspectErr)
		if err == nil {
			err = fmt.Errorf("effect could not be verified")
		}
		// No fingerprint on an ambiguous apply: another writer may own the effect.
		setState(r, Unknown, err)
		if clean && inspectErr == nil && ctx.Err() == nil {
			setState(r, Failed, err)
		}
	}
	if saveErr := m.save(ctx, j); saveErr != nil {
		return status(j), saveErr
	}
	return status(j), err
}

// Stop first persists terminal intent, then attempts every local target. A failed
// cleanup stays retryable and never releases its claim or reports recovery.
func (m *Manager) Stop(ctx context.Context, id string) (NodeStatus, error) {
	if _, err := m.load(ctx, id); err != nil {
		return NodeStatus{}, err
	}
	result, err := m.db.ExecContext(ctx, "UPDATE recovery_runs_v1 SET stop = 1 WHERE id = ?", id)
	if err != nil {
		return NodeStatus{}, err
	}
	n, err := result.RowsAffected()
	if err != nil {
		return NodeStatus{}, err
	}
	if n == 0 {
		return NodeStatus{}, sql.ErrNoRows
	}
	unlock, err := m.lock(ctx)
	if err != nil {
		return NodeStatus{}, err
	}
	defer unlock()
	j, err := m.load(ctx, id)
	if err != nil {
		return NodeStatus{}, err
	}
	for i := range j.Records {
		r := &j.Records[i]
		if r.Status.State == Recovered {
			if _, err = m.db.ExecContext(ctx, "DELETE FROM recovery_claims_v1 WHERE resource = ? AND run_id = ? AND target_id = ?", resourceKey(r.Identity), id, r.Target.ID); err != nil {
				return status(j), err
			}
			continue
		}
		var cleanupErr error
		if r.Status.State != Registered && r.Status.State != Failed {
			if r.Fingerprint == "" {
				// A killed injector could have left a child process. A clean read alone is
				// not proof that no late effect can appear after an interrupted operation.
				cleanupErr = fmt.Errorf("injection outcome is unknown; no durable effect receipt")
			} else {
				fp, clean, e := m.backend.Inspect(ctx, *r)
				cleanupErr = e
				if e == nil && !clean && fp != r.Fingerprint {
					cleanupErr = fmt.Errorf("effect ownership cannot be verified")
				}
				if cleanupErr == nil {
					setState(r, Recovering, nil)
					if err = m.save(ctx, j); err != nil {
						return status(j), err
					}
					cleanupErr = m.backend.Recover(ctx, *r)
					_, clean, e = m.backend.Inspect(ctx, *r)
					cleanupErr = errors.Join(cleanupErr, e)
					if cleanupErr == nil && !clean {
						cleanupErr = fmt.Errorf("cleanup did not remove the effect")
					}
				}
			}
		}
		if cleanupErr != nil {
			setState(r, Unknown, cleanupErr)
		} else {
			setState(r, Recovered, nil)
		}
		if err = m.save(ctx, j); err != nil {
			return status(j), err
		}
		if cleanupErr == nil {
			if _, err = m.db.ExecContext(ctx, "DELETE FROM recovery_claims_v1 WHERE resource = ? AND run_id = ? AND target_id = ?", resourceKey(r.Identity), id, r.Target.ID); err != nil {
				return status(j), err
			}
		}
	}
	// Individual failures are represented in the returned status, not discarded.
	return status(j), nil
}

// Complete checks the planned target set as well as state; an empty or truncated
// node response must never turn into an experiment-wide success.
func Complete(plan Plan, s NodeStatus) bool { return api.RecoveryNodeComplete(plan, s) }

// GuardLegacy keeps pre-existing local network commands serialized with managed
// effects. The caller must hold the returned lock until its command finishes.
func (m *Manager) GuardLegacy(ctx context.Context) (func(), error) {
	unlock, err := m.lock(ctx)
	if err != nil {
		return nil, err
	}
	var n int
	if err = m.db.QueryRowContext(ctx, "SELECT count(*) FROM recovery_claims_v1").Scan(&n); err != nil {
		unlock()
		return nil, err
	}
	if n != 0 {
		unlock()
		return nil, fmt.Errorf("network interface reserved by a recovery-managed execution; stop that execution first")
	}
	return unlock, nil
}
