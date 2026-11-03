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

package v1alpha1

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
)

// RecoveryPlan is the immutable v1 wire contract shared by the CLI and Operator.
// Keep this file identical in the CLI API mirror and the Operator API package.
// It contains identities and fault parameters, never credentials or commands.
type RecoveryPlan struct {
	Version    int              `json:"version"`
	ClusterID  string           `json:"clusterID"`
	CRUID      string           `json:"crUID"`
	Name       string           `json:"name"`
	RunID      string           `json:"runID"`
	Generation int64            `json:"generation"`
	Nodes      []RecoveryNode   `json:"nodes"`
	Targets    []RecoveryTarget `json:"targets"`
}

type RecoveryNode struct {
	UID        string `json:"uid"`
	Name       string `json:"name"`
	Address    string `json:"address"`
	ServerName string `json:"serverName"`
}

type RecoveryTarget struct {
	ExperimentIndex int               `json:"experimentIndex"`
	ID              string            `json:"id"`
	NodeUID         string            `json:"nodeUID"`
	Scope           string            `json:"scope"`
	Namespace       string            `json:"namespace,omitempty"`
	PodName         string            `json:"podName,omitempty"`
	PodUID          string            `json:"podUID,omitempty"`
	ContainerID     string            `json:"containerID,omitempty"`
	Runtime         string            `json:"runtime,omitempty"`
	Action          string            `json:"action"`
	Flags           map[string]string `json:"flags"`
}

// RecoveryTargetStatus separates an observed effect from a stop request.
type RecoveryTargetStatus struct {
	ID        string `json:"id"`
	State     string `json:"state"`
	Error     string `json:"error,omitempty"`
	UpdatedAt string `json:"updatedAt"`
}

type RecoveryNodeStatus struct {
	NodeUID       string                 `json:"nodeUID"`
	RunID         string                 `json:"runID"`
	PlanDigest    string                 `json:"planDigest"`
	StopRequested bool                   `json:"stopRequested"`
	Targets       []RecoveryTargetStatus `json:"targets"`
	Error         string                 `json:"error,omitempty"`
}

type RecoveryStatus struct {
	Prepared      bool                 `json:"prepared,omitempty"`
	Error         string               `json:"error,omitempty"`
	Plan          *RecoveryPlan        `json:"plan,omitempty"`
	StopRequested bool                 `json:"stopRequested,omitempty"`
	Nodes         []RecoveryNodeStatus `json:"nodes,omitempty"`
}

func (in *RecoveryStatus) DeepCopy() *RecoveryStatus {
	if in == nil {
		return nil
	}
	out := *in
	if in.Plan != nil {
		p := *in.Plan
		p.Nodes = append([]RecoveryNode(nil), in.Plan.Nodes...)
		p.Targets = append([]RecoveryTarget(nil), in.Plan.Targets...)
		for i := range p.Targets {
			p.Targets[i].Flags = make(map[string]string, len(in.Plan.Targets[i].Flags))
			for k, v := range in.Plan.Targets[i].Flags {
				p.Targets[i].Flags[k] = v
			}
		}
		out.Plan = &p
	}
	out.Nodes = append([]RecoveryNodeStatus(nil), in.Nodes...)
	for i := range out.Nodes {
		out.Nodes[i].Targets = append([]RecoveryTargetStatus(nil), in.Nodes[i].Targets...)
	}
	return &out
}

// RecoveryPlanDigest binds every node response to the same immutable execution.
func RecoveryPlanDigest(plan RecoveryPlan) string {
	b, _ := json.Marshal(plan)
	sum := sha256.Sum256(b)
	return hex.EncodeToString(sum[:])
}

// RecoveryNodeComplete requires the entire planned local target set.
func RecoveryNodeComplete(plan RecoveryPlan, s RecoveryNodeStatus) bool {
	if s.RunID != plan.RunID || s.PlanDigest != RecoveryPlanDigest(plan) || !s.StopRequested || s.Error != "" {
		return false
	}
	want := map[string]bool{}
	for _, t := range plan.Targets {
		if t.NodeUID == s.NodeUID {
			want[t.ID] = true
		}
	}
	if len(want) == 0 || len(want) != len(s.Targets) {
		return false
	}
	for _, t := range s.Targets {
		if !want[t.ID] || t.State != "recovered" {
			return false
		}
		delete(want, t.ID)
	}
	return len(want) == 0
}
