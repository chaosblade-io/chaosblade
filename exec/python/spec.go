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

package python

import "github.com/chaosblade-io/chaosblade-spec-go/spec"

type CommandModelSpec struct {
	spec.BaseExpModelCommandSpec
}

func NewCommandModelSpec() spec.ExpModelCommandSpec {
	return &CommandModelSpec{
		spec.BaseExpModelCommandSpec{
			ExpActions: []spec.ExpActionCommandSpec{},
			ExpFlags:   []spec.ExpFlagSpec{},
		},
	}
}

func (*CommandModelSpec) Name() string {
	return "python"
}

func (*CommandModelSpec) ShortDesc() string {
	return "Python application experiment"
}

func (*CommandModelSpec) LongDesc() string {
	return "Python application experiment, inject faults into Python applications such as HTTP, gRPC, Kafka, MySQL, Redis"
}

func (*CommandModelSpec) Example() string {
	return "blade create python http throwCustomException --exception RuntimeError --exception-message 'chaos test'"
}
