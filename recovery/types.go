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

// Package recovery owns the node-local lifecycle for registered experiments.
// Network and CLI adapters both use this package; it has no Kubernetes client.
package recovery

import (
	"context"
	"regexp"

	api "github.com/chaosblade-io/chaosblade/apis/chaosblade/v1alpha1"
)

type (
	Plan         = api.RecoveryPlan
	Node         = api.RecoveryNode
	Target       = api.RecoveryTarget
	NodeStatus   = api.RecoveryNodeStatus
	TargetStatus = api.RecoveryTargetStatus
)

const (
	ProtocolVersion = 1
	MaxTargets      = 128
	Registered      = "registered"
	Applying        = "applying"
	Active          = "active"
	Recovering      = "recovering"
	Recovered       = "recovered"
	Unknown         = "unknown"
	Failed          = "failed"
)

var identifier = regexp.MustCompile(`^[a-zA-Z0-9][a-zA-Z0-9_.:-]{0,127}$`)

// Identity pins a network namespace and interface across retries and restarts.
type Identity struct {
	TxQueueLength  int    `json:"txQueueLength"`
	BootID         string `json:"bootID"`
	NetNS          string `json:"netNS"`
	PID            int    `json:"pid"`
	Interface      string `json:"interface"`
	InterfaceIndex int    `json:"interfaceIndex"`
}

// Record stores the last known effect separately from the requested operation.
type Record struct {
	Target      Target       `json:"target"`
	Identity    Identity     `json:"identity"`
	Fingerprint string       `json:"fingerprint,omitempty"`
	Status      TargetStatus `json:"status"`
}

// Backend implements one bounded local inverse operation, without API discovery.
type Backend interface {
	Resolve(ctx context.Context, target Target) (Identity, error)
	Inspect(ctx context.Context, record Record) (string, bool, error)
	Apply(ctx context.Context, record Record) error
	Recover(ctx context.Context, record Record) error
}

func Digest(plan Plan) string { return api.RecoveryPlanDigest(plan) }

func ValidatePlan(plan Plan) error      { return api.ValidateRecoveryPlan(plan) }
func ValidateFault(target Target) error { return api.ValidateRecoveryFault(target) }
