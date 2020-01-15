/*
 * Copyright 1999-2020 Alibaba Group Holding Ltd.
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

package docker

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
	return "docker"
}

func (*CommandModelSpec) ShortDesc() string {
	return "Docker experiment"
}

func (*CommandModelSpec) LongDesc() string {
	return "Docker experiment, for example remove container"
}

func (*CommandModelSpec) Example() string {
	return "blade create docker remove --container-id 65eead213dd3"
}
