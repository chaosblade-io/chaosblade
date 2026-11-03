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
	"fmt"
	"math"
	"net/url"
	"regexp"
	"strconv"
	"strings"
)

var (
	recoveryIdentifier = regexp.MustCompile(`^[a-zA-Z0-9][a-zA-Z0-9_.:-]{0,127}$`)
	recoveryInterface  = regexp.MustCompile(`^[a-zA-Z0-9][a-zA-Z0-9_.:-]{0,14}$`)
	recoveryFlag       = regexp.MustCompile(`^[a-zA-Z0-9_.:,/+-]+$`)
)

func ValidateRecoveryPlan(plan RecoveryPlan) error {
	if plan.Version != 1 || !recoveryIdentifier.MatchString(plan.ClusterID) || !recoveryIdentifier.MatchString(plan.CRUID) || !recoveryIdentifier.MatchString(plan.RunID) || plan.Generation < 1 {
		return fmt.Errorf("invalid recovery plan version or identity")
	}
	if len(plan.Targets) == 0 || len(plan.Targets) > 128 || len(plan.Nodes) == 0 || len(plan.Nodes) > 128 {
		return fmt.Errorf("plan must contain 1..%d targets and nodes", 128)
	}
	nodes := map[string]bool{}
	names := map[string]bool{}
	for _, n := range plan.Nodes {
		u, err := url.Parse(n.Address)
		if !recoveryIdentifier.MatchString(n.UID) || n.Name == "" || names[n.Name] || nodes[n.UID] || err != nil || u.Scheme != "https" || u.Host == "" || u.User != nil || u.RawQuery != "" || u.Fragment != "" || (u.Path != "" && u.Path != "/") || n.ServerName == "" {
			return fmt.Errorf("invalid or duplicate recovery node %q", n.UID)
		}
		nodes[n.UID] = true
		names[n.Name] = true
	}
	ids := map[string]bool{}
	for _, t := range plan.Targets {
		if !nodes[t.NodeUID] || !recoveryIdentifier.MatchString(t.ID) || ids[t.ID] {
			return fmt.Errorf("invalid or duplicate target %q", t.ID)
		}
		ids[t.ID] = true
		if t.Scope != "node" && t.Scope != "pod" {
			return fmt.Errorf("unsupported recovery scope %q", t.Scope)
		}
		if t.Scope == "pod" && (!recoveryIdentifier.MatchString(t.PodUID) || !recoveryIdentifier.MatchString(t.ContainerID) || (t.Runtime != "docker" && t.Runtime != "containerd")) {
			return fmt.Errorf("pod recovery requires an exact container and pod identity")
		}
		if err := ValidateRecoveryFault(t); err != nil {
			return err
		}
	}
	return nil
}

func ValidateRecoveryFault(t RecoveryTarget) error {
	if t.Action != "delay" && t.Action != "loss" {
		return fmt.Errorf("unsupported recovery action %q", t.Action)
	}
	if !recoveryInterface.MatchString(t.Flags["interface"]) {
		return fmt.Errorf("invalid network interface")
	}
	allowed := map[string]bool{"interface": true, "time": true, "offset": true, "percent": true}
	if t.Action == "delay" {
		if v, err := strconv.Atoi(t.Flags["time"]); err != nil || v <= 0 || v > 3600000 {
			return fmt.Errorf("delay time must be 1..3600000 milliseconds")
		}
	}
	if t.Action == "loss" {
		if v, err := strconv.ParseFloat(t.Flags["percent"], 64); err != nil || math.IsNaN(v) || math.IsInf(v, 0) || v <= 0 || v > 100 {
			return fmt.Errorf("loss percent must be greater than zero and at most 100")
		}
	}
	if t.Action == "delay" && t.Flags["percent"] != "" || t.Action == "loss" && (t.Flags["time"] != "" || t.Flags["offset"] != "") {
		return fmt.Errorf("flag does not belong to the selected action")
	}
	for _, key := range []string{"offset"} {
		if raw := t.Flags[key]; raw != "" {
			v, err := strconv.Atoi(raw)
			max := 100
			if key == "offset" {
				max = 3600000
			}
			if err != nil || v < 0 || v > max {
				return fmt.Errorf("invalid %s", key)
			}
		}
	}
	for k, v := range t.Flags {
		if !allowed[k] || !recoveryFlag.MatchString(v) || strings.HasPrefix(v, "-") {
			return fmt.Errorf("unsupported or invalid recovery flag %q", k)
		}
	}
	return nil
}
