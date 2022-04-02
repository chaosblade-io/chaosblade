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

package cmd

import (
	"context"
	"fmt"
	"github.com/chaosblade-io/chaosblade-spec-go/spec"

	"github.com/spf13/cobra"

	"github.com/chaosblade-io/chaosblade/exec/jvm"
)

type QueryJvmCommand struct {
	baseCommand
}

func (qjc *QueryJvmCommand) Init() {
	qjc.command = &cobra.Command{
		Use:   "jvm <UID>",
		Short: "Query hit counts of the specify experiment",
		Long:  "Query hit counts of the specify experiment",
		Args:  cobra.ExactArgs(1),
		RunE: func(cmd *cobra.Command, args []string) error {
			ctx := context.WithValue(context.Background(), spec.Uid,  args[0])
			return qjc.queryJvmExpStatus(ctx, cmd)
		},
		Example: qjc.queryJvmExample(),
	}
}

func (qjc *QueryJvmCommand) queryJvmExample() string {
	return `blade query jvm 29c3f9dab4abbc79`
}

// queryJvmExpStatus by uid
func (qjc *QueryJvmCommand) queryJvmExpStatus(ctx context.Context, command *cobra.Command) error {
	response := jvm.NewExecutor().QueryStatus(ctx)
	if response.Success {
		command.Println(response.Print())
	} else {
		return fmt.Errorf(response.Error())
	}
	return nil
}
