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
	"context"
	"flag"
	"fmt"
	"strings"
	"sync"
	"time"

	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime/serializer"
	"k8s.io/apimachinery/pkg/types"
	"k8s.io/client-go/kubernetes/scheme"
	"k8s.io/client-go/rest"
	"k8s.io/client-go/tools/cache"
	"k8s.io/client-go/tools/clientcmd"
	"k8s.io/klog/v2"
	"sigs.k8s.io/controller-runtime/pkg/client"

	"github.com/chaosblade-io/chaosblade-operator/pkg/apis/chaosblade/v1alpha1"
	"github.com/chaosblade-io/chaosblade-spec-go/log"
	"github.com/chaosblade-io/chaosblade-spec-go/spec"
	"github.com/chaosblade-io/chaosblade-spec-go/util"
)

const (
	QueryCreate  = "create"
	QueryDestroy = "destroy"

	DefaultWaitingTime = "20s"
)

func init() {
	// disable printing of client-go logs
	klog.InitFlags(nil)
	flag.Set("v", "0")
	flag.Parse()
}

type Executor struct{}

func NewExecutor() spec.Executor {
	return &Executor{}
}

func (*Executor) Name() string {
	return "k8s"
}

func (e *Executor) SetChannel(channel spec.Channel) {
}

var (
	cli   client.Client
	cliMu sync.Mutex
)

func QueryStatus(ctx context.Context, operation, kubeconfig, proxyURL, token string) (*spec.Response, bool) {
	uid := ctx.Value(spec.Uid).(string)
	client, err := getClient(kubeconfig, proxyURL, token)
	if err != nil {
		log.Errorf(ctx, "%s", spec.K8sExecFailed.Sprintf("getClient", err))
		return spec.ResponseFailWithResult(spec.K8sExecFailed, CreateConfirmFailedStatusResult(uid, spec.K8sExecFailed.Sprintf("getClient", err)),
			"getClient", err), true
	}
	chaosblade, err := get(client, uid)
	if err != nil {
		if strings.Contains(err.Error(), "not found") && QueryDestroy == operation {
			return spec.ReturnSuccess(CreateConfirmDestroyedStatusResult(uid)), true
		}
		errMsg := spec.K8sExecFailed.Sprintf("getClient", err)
		log.Errorf(ctx, "%s", errMsg)
		return spec.ResponseFailWithResult(spec.K8sExecFailed, CreateConfirmFailedStatusResult(uid, errMsg), "getClient", err), true
	}

	if chaosblade == nil && operation != QueryDestroy {
		errMsg := "the experiment not found"
		log.Errorf(ctx, "%s", errMsg)
		return spec.ResponseFailWithResult(spec.K8sExecFailed, CreateConfirmFailedStatusResult(uid, errMsg), "get", errMsg), true
	}

	if chaosblade.Status.Phase == v1alpha1.ClusterPhaseRunning {
		if operation == QueryCreate {
			statusResult := CreateStatusResult(uid, true, "", chaosblade.Status.ExpStatuses)
			return spec.ReturnSuccess(statusResult), completed(operation, statusResult)
		}
		errMsg := spec.UnexpectedStatus.Sprintf("destroyed", chaosblade.Status.Phase)
		statusResult := CreateStatusResult(uid, false, errMsg, chaosblade.Status.ExpStatuses)
		log.Errorf(ctx, "%s", errMsg)
		return spec.ResponseFailWithResult(spec.UnexpectedStatus, statusResult, "running", chaosblade.Status.Phase),
			completed(operation, statusResult)
	}
	if chaosblade.Status.Phase == v1alpha1.ClusterPhaseDestroyed {
		if operation == QueryCreate {
			errMsg := spec.UnexpectedStatus.Sprintf("running", chaosblade.Status.Phase)
			statusResult := CreateStatusResult(uid, false, errMsg, chaosblade.Status.ExpStatuses)
			log.Errorf(ctx, "%s", errMsg)
			return spec.ResponseFailWithResult(spec.UnexpectedStatus, statusResult, "running", chaosblade.Status.Phase),
				completed(operation, statusResult)
		}
		statusResult := CreateStatusResult(uid, true, "", chaosblade.Status.ExpStatuses)
		return spec.ReturnSuccess(statusResult), completed(operation, statusResult)
	}

	statusResult := CreateStatusResult(uid, false, spec.UnexpectedStatus.Sprintf(operation, chaosblade.Status.Phase),
		chaosblade.Status.ExpStatuses)
	log.Errorf(ctx, "%s", fmt.Sprintf("chaosblade result: %v", chaosblade.Status.ExpStatuses))
	if len(statusResult.Statuses) > 0 {
		statuses := statusResult.Statuses
		if statuses[0].Code > 0 {
			return spec.ResponseFail(statuses[0].Code, statusResult.Error, statusResult), completed(operation, statusResult)
		}
	}
	return spec.ResponseFail(spec.UnexpectedStatus.Code, statusResult.Error, statusResult), completed(operation, statusResult)
}

func (e *Executor) Exec(uid string, ctx context.Context, expModel *spec.ExpModel) *spec.Response {
	// kubewiz 模式判断（最高优先级）
	kubewizURL := expModel.ActionFlags[KubewizURLFlag.Name]
	if kubewizURL != "" {
		return e.execViaKubewiz(uid, ctx, expModel, kubewizURL)
	}

	config := expModel.ActionFlags[KubeConfigFlag.Name]
	if config != "" {
		if ok := util.IsExist(config); !ok {
			config = ""
		}
	}
	proxyURL := expModel.ActionFlags[KubectlProxyFlag.Name]
	token := expModel.ActionFlags[TokenFlag.Name]
	client, err := getClient(config, proxyURL, token)
	if err != nil {
		log.Errorf(ctx, "%s", spec.K8sExecFailed.Sprintf("getClient", err))
		return spec.ResponseFailWithFlags(spec.K8sExecFailed, "getClient", err)
	}
	var response *spec.Response
	var completed bool
	var operation string
	if suid, ok := spec.IsDestroy(ctx); ok {
		if suid == spec.UnknownUid {
			log.Errorf(ctx, "%s",
				spec.ParameterInvalid.Sprintf("suid", spec.UnknownUid, "not support destroy k8s experiments without uid"))
			return spec.ResponseFailWithFlags(spec.ParameterInvalid, "suid", spec.UnknownUid,
				"not support destroy k8s experiments without uid")
		}
		operation = QueryDestroy
		response, completed = e.destroy(ctx, client, config, proxyURL, token)
	} else {
		if expModel.ActionProcessHang {
			expModel.ActionFlags["cgroup-root"] = "/host-sys/fs/cgroup"
		}
		operation = QueryCreate
		response, completed = e.create(ctx, client, config, proxyURL, token, uid, expModel)
	}

	var duration time.Duration
	waitingTime := expModel.ActionFlags[WaitingTimeFlag.Name]
	if waitingTime == "" {
		waitingTime = DefaultWaitingTime
	}
	d, err := time.ParseDuration(waitingTime)
	if err != nil {
		d, _ = time.ParseDuration(DefaultWaitingTime)
	}
	duration = d
	if duration > time.Second {
		ctx, cancel := context.WithTimeout(ctx, duration)
		defer cancel()
		ticker := time.NewTicker(time.Second)
		defer ticker.Stop()

		for {
			select {
			case <-ctx.Done():
				return response
			case <-ticker.C:
				response, completed = QueryStatus(ctx, operation, config, proxyURL, token)
				if completed {
					return response
				}
			}
		}
	}
	return response
}

func (*Executor) destroy(ctx context.Context, cli client.Client, config, proxyURL, token string) (*spec.Response, bool) {
	err := delete(ctx, cli)
	if err != nil {
		errMsg := spec.K8sExecFailed.Sprintf("delete", err)
		log.Errorf(ctx, "%s", errMsg)
		uid := ctx.Value(spec.DestroyKey).(string)
		return spec.ResponseFailWithResult(spec.K8sExecFailed, CreateConfirmFailedStatusResult(uid, errMsg), "delete", err), true
	}
	// 查询资源
	return QueryStatus(ctx, QueryDestroy, config, proxyURL, token)
}

func (e *Executor) create(ctx context.Context, cli client.Client, kubeconfig, proxyURL, token, uid string, expModel *spec.ExpModel) (*spec.Response, bool) {
	log.Infof(ctx, "create uid: %s, target: %s, scope: %s, action: %s", uid, expModel.Target, expModel.Scope, expModel.ActionName)
	// log.Info("create", "uid", uid, "target", expModel.Target, "scope", expModel.Scope, "action", expModel.ActionName)
	chaosBladeObj := convertExpModelToChaosBladeObject(uid, expModel)
	var err error
	resource, err := create(cli, &chaosBladeObj)
	if err != nil {
		errMsg := spec.K8sExecFailed.Sprintf("create", err)
		log.Errorf(ctx, "%s", errMsg)
		return spec.ResponseFailWithResult(spec.K8sExecFailed, CreateConfirmFailedStatusResult(uid, errMsg), "create", err), true
	}
	if resource.Status.Phase == v1alpha1.ClusterPhaseRunning {
		return spec.ReturnSuccess(CreateStatusResult(uid, true, "", resource.Status.ExpStatuses)), true
	}
	response, flag := QueryStatus(ctx, QueryCreate, kubeconfig, proxyURL, token)
	return response, flag
}

func (e *Executor) checkCreateStatus(ctx context.Context, uid string, store cache.Store, cli client.Client,
	resource *v1alpha1.ChaosBlade,
) *spec.Response {
	var chaosblade *v1alpha1.ChaosBlade
	item, _, err := store.GetByKey(resource.Name)
	if err != nil || item == nil {
		chaosblade, err = get(cli, resource.Name)
	} else {
		chaosblade = item.(*v1alpha1.ChaosBlade)
	}
	log.Debugf(ctx, "chaosblade: %+v", chaosblade)
	if chaosblade.Status.Phase == v1alpha1.ClusterPhaseRunning {
		return spec.ReturnSuccess(CreateStatusResult(uid, true, "", chaosblade.Status.ExpStatuses))
	}
	errMsg := spec.UnexpectedStatus.Sprintf("running", chaosblade.Status.Phase)
	log.Errorf(ctx, "%s", errMsg)
	return spec.ResponseFailWithResult(spec.UnexpectedStatus, CreateStatusResult(uid, false, errMsg, chaosblade.Status.ExpStatuses),
		"running", chaosblade.Status.Phase)
}

type StatusResult struct {
	Uid      string                    `json:"uid"`
	Success  bool                      `json:"success"`
	Error    string                    `json:"error"`
	Statuses []v1alpha1.ResourceStatus `json:"statuses"`
}

func CreateStatusResult(uid string, success bool, errMsg string, expStatus []v1alpha1.ExperimentStatus) StatusResult {
	statuses := make([]v1alpha1.ResourceStatus, 0)
	if expStatus != nil && len(expStatus) > 0 {
		experimentStatus := expStatus[0]
		statuses = experimentStatus.ResStatuses
		if statuses == nil || len(statuses) == 0 {
			statuses = append(statuses, v1alpha1.ResourceStatus{
				State:   experimentStatus.State,
				Error:   experimentStatus.Error,
				Success: experimentStatus.Success,
			})
		} else {
			if statuses[0].Error != "" {
				errMsg = statuses[0].Error
			}
		}
	}
	return StatusResult{
		Uid:      uid,
		Success:  success,
		Error:    errMsg,
		Statuses: statuses,
	}
}

func CreateConfirmFailedStatusResult(uid, errMsg string) StatusResult {
	statuses := make([]v1alpha1.ResourceStatus, 0)
	statuses = append(statuses, v1alpha1.ResourceStatus{
		Id:      uid,
		State:   string(v1alpha1.ClusterPhaseError),
		Error:   errMsg,
		Success: false,
	})
	return StatusResult{
		Uid:      uid,
		Success:  false,
		Error:    errMsg,
		Statuses: statuses,
	}
}

func CreateConfirmDestroyedStatusResult(uid string) StatusResult {
	statuses := make([]v1alpha1.ResourceStatus, 0)
	statuses = append(statuses, v1alpha1.ResourceStatus{
		Id:      uid,
		State:   v1alpha1.DestroyedState,
		Success: true,
	})
	return StatusResult{
		Uid:      uid,
		Success:  true,
		Statuses: statuses,
	}
}

func convertExpModelToChaosBladeObject(uid string, expModel *spec.ExpModel) v1alpha1.ChaosBlade {
	experimentSpec := v1alpha1.ExperimentSpec{
		Scope:    expModel.Scope,
		Target:   expModel.Target,
		Action:   expModel.ActionName,
		Desc:     fmt.Sprintf("created by blade command"),
		Matchers: convertFlagsToResourceFlags(expModel.ActionFlags),
	}
	chaosBladeSpec := v1alpha1.ChaosBladeSpec{
		Experiments: []v1alpha1.ExperimentSpec{experimentSpec},
	}
	chaosBlade := v1alpha1.ChaosBlade{
		TypeMeta: metav1.TypeMeta{
			APIVersion: "chaosblade.io/v1alpha1",
			Kind:       "ChaosBlade",
		},
		ObjectMeta: metav1.ObjectMeta{
			Name: uid,
		},
		Spec: chaosBladeSpec,
	}
	return chaosBlade
}

func convertFlagsToResourceFlags(flags map[string]string) []v1alpha1.FlagSpec {
	flagSpecs := make([]v1alpha1.FlagSpec, 0)
	for name, values := range flags {
		if name == KubeConfigFlag.Name || name == WaitingTimeFlag.Name || name == KubectlProxyFlag.Name || name == TokenFlag.Name ||
			name == KubewizURLFlag.Name || name == ClusterUUIDFlag.Name || name == KubewizTokenFlag.Name {
			continue
		}
		valueArr := strings.Split(values, ",")
		flagSpecs = append(flagSpecs, v1alpha1.FlagSpec{
			Name:  name,
			Value: valueArr,
		})
	}
	return flagSpecs
}

func get(cli client.Client, name string) (result *v1alpha1.ChaosBlade, err error) {
	result = &v1alpha1.ChaosBlade{}
	err = cli.Get(context.TODO(), types.NamespacedName{Name: name}, result)
	result.TypeMeta = metav1.TypeMeta{
		APIVersion: "chaosblade.io/v1alpha1",
		Kind:       "ChaosBlade",
	}
	return result, err
}

func create(cli client.Client, chaosblade *v1alpha1.ChaosBlade) (result *v1alpha1.ChaosBlade, err error) {
	err = cli.Create(context.TODO(), chaosblade)
	if err != nil {
		return nil, err
	}
	return get(cli, chaosblade.Name)
}

func delete(ctx context.Context, cli client.Client) error {
	uid := ctx.Value(spec.DestroyKey).(string)
	objectMeta := metav1.ObjectMeta{Name: uid}
	return cli.Delete(context.TODO(), &v1alpha1.ChaosBlade{
		TypeMeta: metav1.TypeMeta{
			APIVersion: "chaosblade.io/v1alpha1",
			Kind:       "ChaosBlade",
		},
		ObjectMeta: objectMeta,
	})
}

func update(cli client.Client, chaosblade *v1alpha1.ChaosBlade) error {
	return cli.Update(context.TODO(), chaosblade)
}

func getClient(kubeconfig, proxyURL, token string) (client.Client, error) {
	cliMu.Lock()
	defer cliMu.Unlock()
	if cli != nil {
		return cli, nil
	}
	c, err := newClient(kubeconfig, proxyURL, token)
	if err != nil {
		return nil, err
	}
	cli = c
	return cli, nil
}

// execViaKubewiz 通过 kubewiz-core 任务委托模式执行 K8s 操作
func (e *Executor) execViaKubewiz(uid string, ctx context.Context, expModel *spec.ExpModel, kubewizURL string) *spec.Response {
	// 校验 uid 非空，避免后续生成空标识的 CR
	if uid == "" {
		return spec.ResponseFailWithFlags(spec.ParameterLess, "uid")
	}
	clusterUUID := expModel.ActionFlags[ClusterUUIDFlag.Name]
	kubewizToken := expModel.ActionFlags[KubewizTokenFlag.Name]

	if clusterUUID == "" {
		return spec.ResponseFailWithFlags(spec.ParameterLess, ClusterUUIDFlag.Name)
	}
	if kubewizToken == "" {
		return spec.ResponseFailWithFlags(spec.ParameterLess, KubewizTokenFlag.Name)
	}

	kc := NewKubewizClient(kubewizURL, clusterUUID, kubewizToken)

	// destroy 走原有逻辑（单任务）
	if suid, ok := spec.IsDestroy(ctx); ok {
		taskUUID, err := kc.SubmitDestroyTask(suid)
		if err != nil {
			log.Errorf(ctx, "submit kubewiz destroy task failed: %v", err)
			return spec.ResponseFailWithFlags(spec.K8sExecFailed, "kubewiz-submit", err)
		}
		log.Infof(ctx, "kubewiz destroy task submitted: %s", taskUUID)

		waitingTime := expModel.ActionFlags[WaitingTimeFlag.Name]
		if waitingTime == "" {
			waitingTime = DefaultWaitingTime
		}
		duration, parseErr := time.ParseDuration(waitingTime)
		if parseErr != nil {
			duration = 20 * time.Second
		}

		pollCtx, cancel := context.WithTimeout(ctx, duration)
		defer cancel()
		task, pollErr := kc.PollTaskUntilDone(pollCtx, taskUUID, 2*time.Second)
		if pollErr != nil {
			return spec.ResponseFailWithFlags(spec.K8sExecFailed, "kubewiz-timeout",
				fmt.Sprintf("destroy task %s not completed within %s", taskUUID, waitingTime))
		}
		return kc.ConvertTaskToResponse(ctx, task, suid, "destroy")
	}

	// === create 两阶段流程 ===

	// 阶段1: 提交 kubectl apply 创建 CR
	createTaskUUID, err := kc.SubmitCreateTask(uid, expModel)
	if err != nil {
		log.Errorf(ctx, "submit kubewiz create task failed: %v", err)
		return spec.ResponseFailWithFlags(spec.K8sExecFailed, "kubewiz-submit", err)
	}
	log.Infof(ctx, "kubewiz create task submitted: %s", createTaskUUID)

	// 等待创建任务完成
	waitingTime := expModel.ActionFlags[WaitingTimeFlag.Name]
	if waitingTime == "" {
		waitingTime = DefaultWaitingTime
	}
	duration, parseErr := time.ParseDuration(waitingTime)
	if parseErr != nil {
		duration = 20 * time.Second
	}

	createCtx, createCancel := context.WithTimeout(ctx, duration)
	defer createCancel()
	createTask, pollErr := kc.PollTaskUntilDone(createCtx, createTaskUUID, 2*time.Second)
	if pollErr != nil {
		return spec.ResponseFailWithFlags(spec.K8sExecFailed, "kubewiz-timeout",
			fmt.Sprintf("create task %s not completed within %s", createTaskUUID, waitingTime))
	}
	// 如果创建任务本身失败（比如 kubectl apply 报错）
	if createTask.Status == kubewizTaskFailed {
		errMsg := ""
		// 优先从 artifact 获取实际执行错误
		if len(createTask.ArtifactURLs) > 0 {
			if output, err := kc.GetArtifact(ctx, createTask.ArtifactURLs[0]); err == nil && strings.TrimSpace(output) != "" {
				errMsg = strings.TrimSpace(output)
			}
		}
		if errMsg == "" {
			errMsg = createTask.ErrorMessage
		}
		if errMsg == "" {
			errMsg = "kubectl apply failed"
		}
		return spec.ResponseFailWithFlags(spec.K8sExecFailed, "kubewiz-create", errMsg)
	}
	// 创建任务被取消，直接返回失败
	if createTask.Status == kubewizTaskCancelled {
		return spec.ResponseFailWithFlags(spec.K8sExecFailed, "kubewiz-create", "task cancelled")
	}

	// 阶段2: 循环查询 CR 状态直到终态
	var lastResponse *spec.Response
	queryDeadline := time.Now().Add(duration)
	for time.Now().Before(queryDeadline) {
		time.Sleep(2 * time.Second)

		queryTaskUUID, err := kc.SubmitQueryTask(uid)
		if err != nil {
			log.Warnf(ctx, "submit kubewiz query task failed: %v", err)
			continue
		}

		queryCtx, queryCancel := context.WithTimeout(ctx, 15*time.Second)
		queryTask, pollErr := kc.PollTaskUntilDone(queryCtx, queryTaskUUID, 2*time.Second)
		queryCancel()

		if pollErr != nil {
			log.Warnf(ctx, "query task poll timeout: %v", pollErr)
			continue
		}
		if queryTask.Status != kubewizTaskCompleted {
			log.Warnf(ctx, "query task failed: %s", queryTask.ErrorMessage)
			continue
		}

		// 获取 artifact（kubectl get -o json 的输出）
		response := kc.ConvertTaskToResponse(ctx, queryTask, uid, "create")
		lastResponse = response

		// 检查是否到达终态
		if !response.Success {
			// 明确失败（phase=Error 或 UnexpectedStatus），直接返回
			return response
		}
		// 检查 result 中的 phase
		if result, ok := response.Result.(map[string]interface{}); ok {
			if phase, ok := result["phase"].(string); ok {
				if phase == "Running" || phase == "Error" {
					return response
				}
			}
		} else {
			// 无法从 response 中提取 phase（如 artifact 获取失败），继续轮询
			log.Warnf(ctx, "unable to extract phase from response result, continue polling")
			continue
		}
	}

	// 超时：优先返回最后一次查询结果，否则返回超时错误
	if lastResponse != nil {
		return spec.ResponseFailWithResult(spec.K8sExecFailed, lastResponse.Result,
			"kubewiz-timeout", fmt.Sprintf("CR %s did not reach terminal phase within %s (last phase included)", uid, waitingTime))
	}
	return spec.ResponseFailWithFlags(spec.K8sExecFailed, "kubewiz-timeout",
		fmt.Sprintf("CR %s did not reach terminal phase within %s", uid, waitingTime))
}

func newClient(kubeConfig, proxyURL, token string) (client.Client, error) {
	var clusterConfig *rest.Config
	var err error

	if proxyURL != "" {
		if token != "" {
			clusterConfig = &rest.Config{
				Host:        proxyURL,
				BearerToken: token,
				TLSClientConfig: rest.TLSClientConfig{
					Insecure: true,
				},
			}
		} else if kubeConfig != "" {
			clientConfig := clientcmd.NewNonInteractiveDeferredLoadingClientConfig(
				&clientcmd.ClientConfigLoadingRules{
					ExplicitPath: kubeConfig,
				},
				&clientcmd.ConfigOverrides{},
			)
			baseConfig, err := clientConfig.ClientConfig()
			if err != nil {
				return nil, err
			}
			clusterConfig = baseConfig
			clusterConfig.Host = proxyURL
			clusterConfig.TLSClientConfig = rest.TLSClientConfig{
				Insecure: true,
			}
		} else {
			clientConfig := clientcmd.NewNonInteractiveDeferredLoadingClientConfig(
				&clientcmd.ClientConfigLoadingRules{},
				&clientcmd.ConfigOverrides{},
			)
			baseConfig, err := clientConfig.ClientConfig()
			if err != nil {
				clusterConfig = &rest.Config{
					Host: proxyURL,
					TLSClientConfig: rest.TLSClientConfig{
						Insecure: true,
					},
				}
			} else {
				clusterConfig = baseConfig
				clusterConfig.Host = proxyURL
				clusterConfig.TLSClientConfig = rest.TLSClientConfig{
					Insecure: true,
				}
			}
		}
	} else if kubeConfig == "" {
		clusterConfig, err = rest.InClusterConfig()
		if err != nil {
			return nil, err
		}
	} else {
		clientConfig := clientcmd.NewNonInteractiveDeferredLoadingClientConfig(
			&clientcmd.ClientConfigLoadingRules{
				ExplicitPath: kubeConfig,
			},
			&clientcmd.ConfigOverrides{},
		)
		clusterConfig, err = clientConfig.ClientConfig()
	}
	if err != nil {
		return nil, err
	}
	clusterConfig.ContentConfig.GroupVersion = &v1alpha1.SchemeGroupVersion
	clusterConfig.APIPath = "/apis"
	clusterConfig.NegotiatedSerializer = serializer.WithoutConversionCodecFactory{CodecFactory: scheme.Codecs}
	clusterConfig.UserAgent = rest.DefaultKubernetesUserAgent()
	scheme, err := v1alpha1.SchemeBuilder.Build()
	if err != nil {
		return nil, err
	}
	return client.New(clusterConfig, client.Options{Scheme: scheme})
}

func completed(operation string, statusResult StatusResult) bool {
	if operation == QueryDestroy {
		return statusResult.Success
	}
	statuses := statusResult.Statuses
	return statuses != nil && len(statuses) > 0
}

func GetChaosBladeByName(name, kubeconfig, proxyURL, token string) (result *v1alpha1.ChaosBlade, err error) {
	client, err := getClient(kubeconfig, proxyURL, token)
	if err != nil {
		return nil, err
	}
	return get(client, name)
}

func RemoveFinalizer(name, kubeconfig, proxyURL, token string) error {
	cli, err := getClient(kubeconfig, proxyURL, token)
	if err != nil {
		return err
	}
	chaosblade, err := get(cli, name)
	if err != nil {
		return err
	}
	chaosblade.Finalizers = []string{}
	return update(cli, chaosblade)
}
