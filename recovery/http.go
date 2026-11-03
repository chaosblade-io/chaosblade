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
	"crypto/tls"
	"crypto/x509"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log"
	"net/http"
	"net/url"
	"os"
	"strings"
	"sync"
	"time"
)

// TLSFiles must use a dedicated recovery CA. A trusted client certificate grants
// stop/read access on this cluster; Kubernetes RBAC is deliberately not consulted.
type TLSFiles struct {
	CA          string `json:"ca"`
	Certificate string `json:"certificate"`
	Key         string `json:"key"`
}

func TLSConfig(files TLSFiles, server bool) (*tls.Config, error) {
	ca, err := os.ReadFile(files.CA)
	if err != nil {
		return nil, err
	}
	pool := x509.NewCertPool()
	if !pool.AppendCertsFromPEM(ca) {
		return nil, fmt.Errorf("invalid recovery CA")
	}
	cert, err := tls.LoadX509KeyPair(files.Certificate, files.Key)
	if err != nil {
		return nil, err
	}
	config := &tls.Config{MinVersion: tls.VersionTLS13, Certificates: []tls.Certificate{cert}}
	if server {
		config.ClientCAs = pool
		config.ClientAuth = tls.RequireAndVerifyClientCert
	} else {
		config.RootCAs = pool
	}
	return config, nil
}

type Snapshot struct {
	Plan   Plan       `json:"plan"`
	Status NodeStatus `json:"status"`
}

// Handler exposes only read and stop operations, never registration or creation.
func (m *Manager) Handler() http.Handler {
	slots := make(chan struct{}, 8)
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		if r.TLS == nil || len(r.TLS.VerifiedChains) == 0 {
			http.Error(w, "client certificate required", http.StatusUnauthorized)
			return
		}
		if r.Header.Get("X-ChaosBlade-Cluster") != m.clusterID || r.Header.Get("X-ChaosBlade-Node") != m.nodeName {
			http.Error(w, "cluster or node identity mismatch", http.StatusForbidden)
			return
		}
		select {
		case slots <- struct{}{}:
			defer func() { <-slots }()
		default:
			http.Error(w, "recovery service busy", http.StatusTooManyRequests)
			return
		}
		ctx, cancel := context.WithTimeout(r.Context(), 45*time.Second)
		defer cancel()
		if r.URL.Path == "/v1/runs" && r.Method == http.MethodGet {
			summaries, err := m.Runs(ctx)
			if err != nil {
				http.Error(w, err.Error(), http.StatusInternalServerError)
				return
			}
			_ = json.NewEncoder(w).Encode(summaries)
			return
		}
		parts := strings.Split(strings.Trim(r.URL.Path, "/"), "/")
		if len(parts) < 3 || parts[0] != "v1" || parts[1] != "runs" || !identifier.MatchString(parts[2]) {
			http.NotFound(w, r)
			return
		}
		id := parts[2]
		var result interface{}
		var err error
		switch {
		case len(parts) == 3 && r.Method == http.MethodGet:
			var j journal
			j, err = m.load(ctx, id)
			if err == nil {
				var observed NodeStatus
				observed = status(j)
				if r.URL.Query().Get("observe") == "true" {
					observed, err = m.Status(ctx, id)
				}
				result = Snapshot{Plan: j.Plan, Status: observed}
			}
		case len(parts) == 4 && parts[3] == "stop" && r.Method == http.MethodPost:
			if r.ContentLength > 0 || len(r.TransferEncoding) > 0 {
				http.Error(w, "stop accepts no body", http.StatusBadRequest)
				return
			}
			plan, lookupErr := m.Plan(ctx, id)
			if lookupErr != nil {
				err = lookupErr
				break
			}
			if r.Header.Get("X-ChaosBlade-Plan") != Digest(plan) || r.Header.Get("X-ChaosBlade-Node-UID") != localNode(plan, m.nodeName) {
				http.Error(w, "execution plan identity mismatch", http.StatusConflict)
				return
			}
			result, err = m.Stop(ctx, id)
			log.Printf("recovery stop cluster=%q node=%q run=%q client=%q serial=%q error=%v", m.clusterID, m.nodeName, id, r.TLS.PeerCertificates[0].Subject.String(), r.TLS.PeerCertificates[0].SerialNumber.String(), err)
		default:
			http.Error(w, "unsupported recovery operation", http.StatusMethodNotAllowed)
			return
		}
		if err != nil {
			code := http.StatusConflict
			if errors.Is(err, sql.ErrNoRows) {
				code = http.StatusNotFound
			}
			http.Error(w, err.Error(), code)
			return
		}
		_ = json.NewEncoder(w).Encode(result)
	})
}

// Client is an offline client: nodes and identities come from an exported plan,
// not a Kubernetes API call, port-forward, service discovery or token review.
type Client struct{ TLS *tls.Config }

func (c *Client) request(ctx context.Context, plan Plan, node Node, operation string, result interface{}) error {
	u, parseErr := url.Parse(node.Address)
	if parseErr != nil || u.Scheme != "https" || u.Host == "" || u.User != nil || u.RawQuery != "" || u.Fragment != "" || (u.Path != "" && u.Path != "/") || node.ServerName == "" || !identifier.MatchString(node.Name) {
		return fmt.Errorf("invalid recovery endpoint or node identity")
	}
	if c.TLS == nil {
		return fmt.Errorf("recovery TLS configuration required")
	}
	config := c.TLS.Clone()
	config.ServerName = node.ServerName
	transport := &http.Transport{TLSClientConfig: config, Proxy: nil, MaxConnsPerHost: 1, TLSHandshakeTimeout: 5 * time.Second, ResponseHeaderTimeout: 50 * time.Second}
	defer transport.CloseIdleConnections()
	client := &http.Client{Transport: transport, Timeout: 55 * time.Second, CheckRedirect: func(req *http.Request, via []*http.Request) error { return http.ErrUseLastResponse }}
	endpoint := strings.TrimRight(node.Address, "/") + "/v1/runs"
	if plan.RunID != "" {
		endpoint += "/" + plan.RunID
	}
	method := http.MethodGet
	if operation == "stop" {
		method = http.MethodPost
		endpoint += "/stop"
	}
	if operation == "status" {
		endpoint += "?observe=true"
	}
	req, err := http.NewRequestWithContext(ctx, method, endpoint, nil)
	if err != nil {
		return err
	}
	req.Header.Set("X-ChaosBlade-Cluster", plan.ClusterID)
	req.Header.Set("X-ChaosBlade-Node", node.Name)
	req.Header.Set("X-ChaosBlade-Node-UID", node.UID)
	req.Header.Set("X-ChaosBlade-Plan", Digest(plan))
	resp, err := client.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		body, _ := io.ReadAll(io.LimitReader(resp.Body, 2048))
		return fmt.Errorf("node returned %d: %s", resp.StatusCode, strings.TrimSpace(string(body)))
	}
	return json.NewDecoder(io.LimitReader(resp.Body, 1<<20)).Decode(result)
}

// Observe queries a single exact node. It also checks the immutable plan digest.
func (c *Client) Observe(ctx context.Context, plan Plan, node Node) (NodeStatus, error) {
	var snap Snapshot
	if err := c.request(ctx, plan, node, "status", &snap); err != nil {
		return NodeStatus{}, err
	}
	if Digest(snap.Plan) != Digest(plan) || snap.Status.PlanDigest != Digest(plan) || snap.Status.NodeUID != node.UID || snap.Status.RunID != plan.RunID {
		return NodeStatus{}, fmt.Errorf("node returned a different execution identity")
	}
	return snap.Status, nil
}

type Result struct {
	RunID    string       `json:"runID"`
	Complete bool         `json:"complete"`
	Nodes    []NodeStatus `json:"nodes"`
}

// Run visits all nodes even if one is unreachable. Only verified recovery of the
// entire planned target set reports complete; partial results remain visible.
func (c *Client) Run(ctx context.Context, plan Plan, stop bool) (Result, error) {
	result := Result{RunID: plan.RunID, Nodes: make([]NodeStatus, len(plan.Nodes)), Complete: true}
	if err := ValidatePlan(plan); err != nil {
		return result, err
	}
	var wg sync.WaitGroup
	slots := make(chan struct{}, 8)
	for i, node := range plan.Nodes {
		wg.Add(1)
		go func(i int, node Node) {
			defer wg.Done()
			select {
			case slots <- struct{}{}:
				defer func() { <-slots }()
			case <-ctx.Done():
				result.Nodes[i] = NodeStatus{NodeUID: node.UID, Error: ctx.Err().Error()}
				return
			}
			var s NodeStatus
			var err error
			if stop {
				err = c.request(ctx, plan, node, "stop", &s)
			} else {
				s, err = c.Observe(ctx, plan, node)
			}
			if err == nil && (s.NodeUID != node.UID || s.RunID != plan.RunID || s.PlanDigest != Digest(plan)) {
				err = fmt.Errorf("invalid node response identity")
			}
			if err != nil {
				s = NodeStatus{NodeUID: node.UID, RunID: plan.RunID, Error: err.Error()}
			}
			result.Nodes[i] = s
		}(i, node)
	}
	wg.Wait()
	for _, s := range result.Nodes {
		if !Complete(plan, s) {
			result.Complete = false
		}
	}
	for _, s := range result.Nodes {
		if s.Error != "" {
			return result, fmt.Errorf("one or more nodes could not be reached or verified")
		}
	}
	if stop && !result.Complete {
		return result, fmt.Errorf("recovery is incomplete; retry the same run after resolving reported errors")
	}
	return result, nil
}

// Profile is prepared at installation time. Seed nodes provide a replicated plan;
// the client then visits every participant listed in that immutable plan.
type Profile struct {
	ClusterID string   `json:"clusterID"`
	Nodes     []Node   `json:"nodes"`
	TLS       TLSFiles `json:"tls"`
}

type RunSummary struct {
	RunID string `json:"runID"`
	Name  string `json:"name"`
	CRUID string `json:"crUID"`
}

func (m *Manager) Runs(ctx context.Context) ([]RunSummary, error) {
	rows, err := m.db.QueryContext(ctx, "SELECT journal FROM recovery_runs_v1 ORDER BY id")
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	result := []RunSummary{}
	for rows.Next() {
		var raw string
		if err = rows.Scan(&raw); err != nil {
			return nil, err
		}
		var j journal
		if err = json.Unmarshal([]byte(raw), &j); err != nil {
			return nil, err
		}
		result = append(result, RunSummary{RunID: j.Plan.RunID, Name: j.Plan.Name, CRUID: j.Plan.CRUID})
	}
	return result, rows.Err()
}

// Discover works with no Kubernetes connection, including when the fault prevented
// exporting a receipt. Reused experiment names require an explicit run ID.
func (c *Client) Discover(ctx context.Context, profile Profile, name, run string) (Plan, error) {
	if !identifier.MatchString(profile.ClusterID) || len(profile.Nodes) == 0 || len(profile.Nodes) > 128 {
		return Plan{}, fmt.Errorf("invalid recovery profile")
	}
	if name == "" && run == "" {
		return Plan{}, fmt.Errorf("experiment name or run ID is required")
	}
	var failures []error
	for _, node := range profile.Nodes {
		seed := Plan{ClusterID: profile.ClusterID}
		var summaries []RunSummary
		if err := c.request(ctx, seed, node, "read", &summaries); err != nil {
			failures = append(failures, err)
			continue
		}
		matches := []RunSummary{}
		for _, s := range summaries {
			if (run != "" && s.RunID == run) || (run == "" && s.Name == name) {
				matches = append(matches, s)
			}
		}
		if len(matches) == 0 {
			continue
		}
		if len(matches) != 1 {
			return Plan{}, fmt.Errorf("experiment name is ambiguous; choose --run from %v", matches)
		}
		seed.RunID = matches[0].RunID
		var snap Snapshot
		if err := c.request(ctx, seed, node, "read", &snap); err != nil {
			failures = append(failures, err)
			continue
		}
		if err := ValidatePlan(snap.Plan); err != nil {
			return Plan{}, err
		}
		if snap.Plan.ClusterID != profile.ClusterID || snap.Plan.RunID != seed.RunID || snap.Status.PlanDigest != Digest(snap.Plan) || localNode(snap.Plan, node.Name) != snap.Status.NodeUID {
			return Plan{}, fmt.Errorf("discovered plan identity mismatch")
		}
		return snap.Plan, nil
	}
	if err := errors.Join(failures...); err != nil {
		return Plan{}, fmt.Errorf("execution could not be discovered: %w", err)
	}
	return Plan{}, fmt.Errorf("execution was not found on the configured seed nodes")
}
