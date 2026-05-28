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
	"github.com/chaosblade-io/chaosblade-exec-cri/exec"
	"github.com/chaosblade-io/chaosblade-spec-go/spec"
)

var resourceCountFlag = &spec.ExpFlag{
	Name:     "evict-count",
	Desc:     "Count of affected resource",
	NoArgs:   false,
	Required: false,
}

var resourcePercentFlag = &spec.ExpFlag{
	Name:     "evict-percent",
	Desc:     "Percent of affected resource, integer value without %",
	NoArgs:   false,
	Required: false,
}

var resourceNamesFlag = &spec.ExpFlag{
	Name:     "names",
	Desc:     "Resource names, such as pod name. You must add namespace flag for it. Multiple parameters are separated directly by commas",
	NoArgs:   false,
	Required: false,
}

var resourceNamespaceFlag = &spec.ExpFlag{
	Name:     "namespace",
	Desc:     "Namespace, such as default, only one value can be specified",
	NoArgs:   false,
	Required: true,
}

var resourceLabelsFlag = &spec.ExpFlag{
	Name:     "labels",
	Desc:     "Label selector, the relationship between values that are or",
	NoArgs:   false,
	Required: false,
}

var resourceGroupKeyFlag = &spec.ExpFlag{
	Name:     "evict-group",
	Desc:     "Group key from labels",
	NoArgs:   false,
	Required: false,
}

var containerIDsFlag = &spec.ExpFlag{
	Name:     "container-ids",
	Desc:     "Container ids",
	NoArgs:   false,
	Required: false,
}

var containerNamesFlag = &spec.ExpFlag{
	Name:     "container-names",
	Desc:     "Container names",
	NoArgs:   false,
	Required: false,
}

var containerIndexFlag = &spec.ExpFlag{
	Name: "container-index",
	Desc: "Container index, start from 0",
}

var chaosBladePathFlag = &spec.ExpFlag{
	Name: "chaosblade-path",
	Desc: "Chaosblade tool deployment path, default value is /opt. Please select a path with write permission",
}

var chaosBladeDeployModeFlag = &spec.ExpFlag{
	Name: "chaosblade-deploy-mode",
	Desc: "The mode of chaosblade deployment in container, the values are copy and download, the default value is copy which copy tool from the operator to the target container. If you select download mode, the operator will download chaosblade tool from the chaosblade-download-url.",
}

var chaosBladeDownloadURLFlag = &spec.ExpFlag{
	Name: "chaosblade-download-url",
	Desc: "The chaosblade downloaded address. If you use download deployment mode, you must specify the value, or config chaosblade-download-url when deploying the operator",
}

func getResourceCoverageFlags() []spec.ExpFlagSpec {
	return []spec.ExpFlagSpec{
		resourceCountFlag,
		resourcePercentFlag,
	}
}

func getResourceCommonFlags() []spec.ExpFlagSpec {
	return []spec.ExpFlagSpec{
		resourceNamesFlag,
		resourceNamespaceFlag,
		resourceLabelsFlag,
		resourceGroupKeyFlag,
	}
}

func getContainerFlags() []spec.ExpFlagSpec {
	return []spec.ExpFlagSpec{
		containerIDsFlag,
		containerNamesFlag,
		containerIndexFlag,
	}
}

func getChaosBladeFlags() []spec.ExpFlagSpec {
	return []spec.ExpFlagSpec{
		chaosBladePathFlag,
		exec.ChaosBladeOverrideFlag,
		chaosBladeDeployModeFlag,
		chaosBladeDownloadURLFlag,
	}
}
