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
	"flag"
	"testing"
)

// TestInitSilencesKlog verifies the package init() registers klog's flags and
// pins verbosity to 0 so client-go logs stay silent. Loading this test binary
// runs init(); the assertions below also guard against reintroducing a bare
// flag.Parse() there, which used to consume blade's raw argv and reject the
// cobra global flags (--debug/--log-level) before cobra ever ran.
func TestInitSilencesKlog(t *testing.T) {
	v := flag.CommandLine.Lookup("v")
	if v == nil {
		t.Fatal("klog flag -v not registered on flag.CommandLine")
	}
	if got := v.Value.String(); got != "0" {
		t.Errorf("klog verbosity = %s, want 0", got)
	}
}
