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

package kubernetes

import (
	"context"
	"fmt"
	"strings"
	"time"

	"github.com/chaosblade-io/chaosblade-operator/pkg/apis/chaosblade/v1alpha1"
	"github.com/chaosblade-io/chaosblade-spec-go/spec"
	"github.com/chaosblade-io/chaosblade-spec-go/util"
	"github.com/sirupsen/logrus"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime/serializer"
	"k8s.io/apimachinery/pkg/types"
	"k8s.io/client-go/kubernetes/scheme"
	"k8s.io/client-go/rest"
	"k8s.io/client-go/tools/cache"
	"k8s.io/client-go/tools/clientcmd"
	"sigs.k8s.io/controller-runtime/pkg/client"
)

const ResourceName = "chaosblades"
const (
	QueryCreate  = "create"
	QueryDestroy = "destroy"

	DefaultWaitingTime = "20s"
)

type Executor struct {
}

func NewExecutor() spec.Executor {
	return &Executor{}
}

func (*Executor) Name() string {
	return "k8s"
}

func (e *Executor) SetChannel(channel spec.Channel) {
}

var cli client.Client

func QueryStatus(operation, uid, kubeconfig string) (*spec.Response, bool) {
	client, err := getClient(kubeconfig)
	if err != nil {
		return spec.ReturnFailWitResult(spec.Code[spec.K8sInvokeError], err.Error(),
			CreateConfirmFailedStatusResult(uid, err.Error())), true
	}
	chaosblade, err := get(client, uid)
	if err != nil {
		if strings.Contains(err.Error(), "not found") && QueryDestroy == operation {
			return spec.ReturnSuccess(CreateConfirmDestroyedStatusResult(uid)), true
		}
		return spec.ReturnFailWitResult(spec.Code[spec.K8sInvokeError], err.Error(),
			CreateConfirmFailedStatusResult(uid, err.Error())), true
	}

	if chaosblade == nil && operation != QueryDestroy {
		errMsg := "the experiment not found"
		return spec.ReturnFailWitResult(spec.Code[spec.K8sInvokeError], errMsg,
			CreateConfirmFailedStatusResult(uid, errMsg)), true
	}

	if chaosblade.Status.Phase == v1alpha1.ClusterPhaseRunning {
		if operation == QueryCreate {
			statusResult := CreateStatusResult(uid, true, "", chaosblade.Status.ExpStatuses)
			return spec.ReturnSuccess(statusResult), completed(operation, statusResult)
		}
		errMsg := fmt.Sprintf("expected destroyed, but the real value is %v", chaosblade.Status.Phase)
		statusResult := CreateStatusResult(uid, false, errMsg, chaosblade.Status.ExpStatuses)
		return spec.ReturnFailWitResult(spec.Code[spec.StatusError], errMsg, statusResult),
			completed(operation, statusResult)
	}
	if chaosblade.Status.Phase == v1alpha1.ClusterPhaseDestroyed {
		if operation == QueryCreate {
			errMsg := fmt.Sprintf("expected running, but the real value is %v", chaosblade.Status.Phase)
			statusResult := CreateStatusResult(uid, false, errMsg, chaosblade.Status.ExpStatuses)
			return spec.ReturnFailWitResult(spec.Code[spec.StatusError],
				errMsg, statusResult), completed(operation, statusResult)
		}
		statusResult := CreateStatusResult(uid, true, "", chaosblade.Status.ExpStatuses)
		return spec.ReturnSuccess(statusResult), completed(operation, statusResult)
	}

	errMsg := fmt.Sprintf("unexpected status, the real value is %v", chaosblade.Status.Phase)

	statusResult := CreateStatusResult(uid, false, errMsg, chaosblade.Status.ExpStatuses)
	return spec.ReturnFailWitResult(spec.Code[spec.StatusError],
		errMsg, statusResult), completed(operation, statusResult)
}

func (e *Executor) Exec(uid string, ctx context.Context, expModel *spec.ExpModel) *spec.Response {
	config := expModel.ActionFlags[KubeConfigFlag.Name]
	if config != "" {
		if ok := util.IsExist(config); !ok {
			config = ""
		}
	}
	client, err := getClient(config)
	if err != nil {
		return spec.ReturnFailWitResult(spec.Code[spec.K8sInvokeError], err.Error(),
			CreateConfirmFailedStatusResult(uid, err.Error()))
	}

	var response *spec.Response
	var completed bool
	var operation string
	if suid, ok := spec.IsDestroy(ctx); ok {
		if suid == spec.UnknownUid {
			errMsg := "does not support destroy k8s experiments without uid"
			return spec.ReturnFailWitResult(spec.Code[spec.IllegalCommand], errMsg,
				CreateConfirmFailedStatusResult(suid, errMsg))
		}
		operation = QueryDestroy
		response, completed = e.destroy(client, suid, config)
	} else {
		operation = QueryCreate
		response, completed = e.create(client, config, uid, expModel)
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
		ctx, cancel := context.WithTimeout(context.Background(), duration)
		defer cancel()
		ticker := time.NewTicker(time.Second)
	TickerLoop:
		for range ticker.C {
			select {
			case <-ctx.Done():
				ticker.Stop()
				break TickerLoop
			default:
				response, completed = QueryStatus(operation, uid, config)
				if completed {
					return response
				}
			}
		}
	}
	return response
}

func (*Executor) destroy(cli client.Client, uid string, config string) (*spec.Response, bool) {
	err := delete(cli, uid)
	if err != nil {
		return spec.ReturnFailWitResult(spec.Code[spec.K8sInvokeError], err.Error(),
			CreateStatusResult(uid, false, err.Error(), nil)), true
	}
	// 查询资源
	return QueryStatus(QueryDestroy, uid, config)
}

func (e *Executor) create(cli client.Client, kubeconfig string, uid string, expModel *spec.ExpModel) (*spec.Response, bool) {
	logrus.Infof("create uid: %s, target: %s, scope: %s, action: %s", uid, expModel.Target, expModel.Scope, expModel.ActionName)
	//log.Info("create", "uid", uid, "target", expModel.Target, "scope", expModel.Scope, "action", expModel.ActionName)
	chaosBladeObj := convertExpModelToChaosBladeObject(uid, expModel)
	var err error
	resource, err := create(cli, &chaosBladeObj)
	if err != nil {
		return spec.ReturnFailWitResult(spec.Code[spec.K8sInvokeError],
			fmt.Sprintf("create err, %v", err),
			CreateConfirmFailedStatusResult(uid, err.Error())), true
	}
	if resource.Status.Phase == v1alpha1.ClusterPhaseRunning {
		return spec.ReturnSuccess(CreateStatusResult(uid, true, "", resource.Status.ExpStatuses)), true
	}
	return QueryStatus(QueryCreate, uid, kubeconfig)
}

func (e *Executor) checkCreateStatus(uid string, store cache.Store, cli client.Client,
	resource *v1alpha1.ChaosBlade) *spec.Response {
	var chaosblade *v1alpha1.ChaosBlade
	item, _, err := store.GetByKey(resource.Name)
	if err != nil || item == nil {
		chaosblade, err = get(cli, resource.Name)
	} else {
		chaosblade = item.(*v1alpha1.ChaosBlade)
	}
	logrus.Debugf("chaosblade: %+v", chaosblade)
	//log.V(1).Info("chaosblade", "chaosblade", chaosblade)
	if chaosblade.Status.Phase == v1alpha1.ClusterPhaseRunning {
		return spec.ReturnSuccess(CreateStatusResult(uid, true, "", chaosblade.Status.ExpStatuses))
	}
	errMsg := fmt.Sprintf("unexpected status %s", string(chaosblade.Status.Phase))
	return spec.ReturnFailWitResult(spec.Code[spec.K8sInvokeError],
		errMsg, CreateStatusResult(uid, false, errMsg, chaosblade.Status.ExpStatuses))
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
		State:   string(v1alpha1.DestroyedState),
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
		if name == KubeConfigFlag.Name || name == WaitingTimeFlag.Name {
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
	return
}

func create(cli client.Client, chaosblade *v1alpha1.ChaosBlade) (result *v1alpha1.ChaosBlade, err error) {
	err = cli.Create(context.TODO(), chaosblade)
	if err != nil {
		return nil, err
	}
	return get(cli, chaosblade.Name)
}

func delete(cli client.Client, name string) error {
	objectMeta := metav1.ObjectMeta{Name: name}
	return cli.Delete(context.TODO(), &v1alpha1.ChaosBlade{ObjectMeta: objectMeta})
}

func update(cli client.Client, chaosblade *v1alpha1.ChaosBlade) error {
	return cli.Update(context.TODO(), chaosblade)
}

func getClient(kubeconfig string) (client.Client, error) {
	if cli == nil {
		c, err := newClient(kubeconfig)
		if err != nil {
			return nil, err
		}
		cli = c
	}
	return cli, nil
}

func newClient(kubeConfig string) (client.Client, error) {
	var clusterConfig *rest.Config
	var err error
	if kubeConfig == "" {
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

func GetChaosBladeByName(name, kubeconfig string) (result *v1alpha1.ChaosBlade, err error) {
	client, err := getClient(kubeconfig)
	if err != nil {
		return nil, err
	}
	return get(client, name)
}

func RemoveFinalizer(name, kubeconfig string) error {
	cli, err := getClient(kubeconfig)
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
