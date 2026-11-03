//go:build linux

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
	"os"
	"os/exec"
	"path/filepath"
	"testing"
	"time"
)

func TestQdiscReadbackRejectsForeignRulesAndIgnoresReferenceCounts(t *testing.T) {
	first := []byte(`[{"kind":"netem","handle":"8001:","root":true,"refcnt":2,"options":{"delay":{"delay":0.05}}}]`)
	second := []byte(`[{"kind":"netem","handle":"8001:","root":true,"refcnt":15,"options":{"delay":{"delay":0.05}}}]`)
	a, clean, err := qdiscFingerprint(first, "delay")
	if err != nil || clean {
		t.Fatal(a, clean, err)
	}
	b, _, err := qdiscFingerprint(second, "delay")
	if err != nil || a != b {
		t.Fatal("reference count invalidated ownership")
	}
	if _, _, err = qdiscFingerprint(first, "loss"); err == nil {
		t.Fatal("missing requested action was accepted")
	}
	if _, _, err = qdiscFingerprint([]byte(`[{"kind":"fq_codel","root":true}]`), "delay"); err == nil {
		t.Fatal("foreign qdisc accepted")
	}
}

func TestCommandCancellationStopsDescendants(t *testing.T) {
	marker := filepath.Join(t.TempDir(), "late-effect")
	ctx, cancel := context.WithTimeout(context.Background(), 50*time.Millisecond)
	defer cancel()
	// The filename is passed as an argument, never interpolated into shell code.
	cmd := exec.CommandContext(ctx, "/bin/sh", "-c", `(sleep 0.2; touch "$1") & wait`, "test", marker)
	configureNetworkCommand(cmd)
	if err := cmd.Run(); err == nil {
		t.Fatal("expected command cancellation")
	}
	time.Sleep(250 * time.Millisecond)
	if _, err := os.Stat(marker); !os.IsNotExist(err) {
		t.Fatal("descendant survived cancellation")
	}
}
