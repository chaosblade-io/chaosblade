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
	"context"
	"errors"

	"github.com/spf13/cobra"

	"github.com/chaosblade-io/chaosblade-spec-go/spec"

	"github.com/chaosblade-io/chaosblade/exec/kubernetes"
)

type QueryK8sCommand struct {
	baseCommand
	kubeconfig   string
	proxyURL     string
	token        string
	kubewizURL   string
	clusterUUID  string
	kubewizToken string
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
	q.command.Flags().StringVar(&q.proxyURL, "kubectl-proxy", "", "Kubectl proxy URL for accessing Kubernetes API, e.g., http://localhost:8001")
	q.command.Flags().StringVar(&q.token, "token", "", "Bearer token for Kubernetes API authentication")
	q.command.Flags().StringVar(&q.kubewizURL, "kubewiz-url", "", "Kubewiz core service URL for delegated K8s operations")
	q.command.Flags().StringVar(&q.clusterUUID, "cluster-uuid", "", "Target cluster UUID in kubewiz")
	q.command.Flags().StringVar(&q.kubewizToken, "kubewiz-token", "", "Token for kubewiz-core authentication")
}

func (q *QueryK8sCommand) queryK8sExample() string {
	return `blade query k8s create 29c3f9dab4abbc79

# 使用 kubectl proxy 查询状态
blade query k8s create 29c3f9dab4abbc79 --kubectl-proxy http://localhost:8001

# 使用 kubewiz 通道查询状态
blade query k8s create 29c3f9dab4abbc79 --kubewiz-url https://kubewiz.example.com --cluster-uuid xxx --kubewiz-token yyy`
}

// queryK8sExpStatus by uid
func (q *QueryK8sCommand) queryK8sExpStatus(command *cobra.Command, cmd, uid string) error {
	ctx := context.WithValue(context.Background(), spec.Uid, uid)

	var response *spec.Response
	if q.kubewizURL != "" {
		if q.clusterUUID == "" {
			return errors.New("--cluster-uuid is required when using --kubewiz-url")
		}
		if q.kubewizToken == "" {
			return errors.New("--kubewiz-token is required when using --kubewiz-url")
		}
		response = kubernetes.QueryStatusViaKubewiz(ctx, cmd, q.kubewizURL, q.clusterUUID, q.kubewizToken, uid)
	} else {
		response, _ = kubernetes.QueryStatus(ctx, cmd, q.kubeconfig, q.proxyURL, q.token)
	}
	command.Println(response.Print())
	if !response.Success {
		return errors.New(response.Error())
	}
	return nil
}
