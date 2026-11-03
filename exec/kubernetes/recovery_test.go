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

package kubernetes

import (
	"testing"

	"github.com/chaosblade-io/chaosblade-spec-go/spec"
)

func TestRecoveryOptInIsMetadataAndNeverAnExecutorFlag(t *testing.T) {
	for _, enabled := range []string{"true", "false", ""} {
		model := &spec.ExpModel{Scope: "pod", Target: "network", ActionName: "delay", ActionFlags: map[string]string{"recovery": enabled, "interface": "eth0", "time": "50"}}
		resource := convertExpModelToChaosBladeObject("example", model)
		if (resource.Annotations["chaosblade.io/recovery"] == "true") != (enabled == "true") {
			t.Fatal("incorrect recovery opt-in")
		}
		for _, matcher := range resource.Spec.Experiments[0].Matchers {
			if matcher.Name == "recovery" {
				t.Fatal("control flag leaked into the fault executor")
			}
		}
	}
}
