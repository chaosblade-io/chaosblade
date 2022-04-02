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

	"github.com/chaosblade-io/chaosblade/exec/kubernetes"
)

type QueryK8sCommand struct {
	baseCommand
	kubeconfig string
}

func (q *QueryK8sCommand) Init() {
	q.command = &cobra.Command{
		Use:   "k8s <UID>",
		Short: "Query status of the specify experiment by uid",
		Long:  "Query status of the specify experiment by uid",
		Args:  cobra.ExactArgs(2),
		RunE: func(cmd *cobra.Command, args []string) error {
			return q.queryK8sExpStatus(cmd, args[0], args[1])
		},
		Example: q.queryK8sExample(),
	}
	q.command.Flags().StringVarP(&q.kubeconfig, "kubeconfig", "k", "", "the kubeconfig path")
}

func (q *QueryK8sCommand) queryK8sExample() string {
	return `blade query k8s create 29c3f9dab4abbc79`
}

// queryK8sExpStatus by uid
func (q *QueryK8sCommand) queryK8sExpStatus(command *cobra.Command, cmd, uid string) error {
	ctx := context.WithValue(context.Background(), spec.Uid, uid)
	response, _ := kubernetes.QueryStatus(ctx, cmd, q.kubeconfig)
	if response.Success {
		command.Println(response.Print())
	} else {
		return fmt.Errorf(response.Error())
	}
	return nil
}
