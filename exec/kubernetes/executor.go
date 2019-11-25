package kubernetes

import (
	"context"
	"fmt"
	"strings"
	"time"

	"github.com/sirupsen/logrus"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime/serializer"
	"k8s.io/client-go/kubernetes/scheme"
	"k8s.io/client-go/rest"
	"k8s.io/client-go/tools/cache"
	"k8s.io/client-go/tools/clientcmd"

	"github.com/chaosblade-io/chaosblade-operator/pkg/apis/chaosblade/v1alpha1"
	"github.com/chaosblade-io/chaosblade-spec-go/spec"
	"github.com/chaosblade-io/chaosblade-spec-go/util"
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

func QueryStatus(operation, uid, kubeconfig string) (*spec.Response, bool) {
	client, err := newClient(kubeconfig)
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
	client, err := newClient(config)
	if err != nil {
		return spec.ReturnFailWitResult(spec.Code[spec.K8sInvokeError], err.Error(),
			CreateConfirmFailedStatusResult(uid, err.Error()))
	}

	var response *spec.Response
	var completed bool
	var operation string
	if suid, ok := spec.IsDestroy(ctx); ok {
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

func (*Executor) destroy(client rest.Interface, uid string, config string) (*spec.Response, bool) {
	err := delete(client, uid, &metav1.DeleteOptions{})
	if err != nil {
		return spec.ReturnFailWitResult(spec.Code[spec.K8sInvokeError], err.Error(),
			CreateStatusResult(uid, false, err.Error(), nil)), true
	}
	// 查询资源
	return QueryStatus(QueryDestroy, uid, config)
}

func (e *Executor) create(client rest.Interface, kubeconfig string, uid string, expModel *spec.ExpModel) (*spec.Response, bool) {
	logrus.Infof("create uid: %s, target: %s, scope: %s, action: %s", uid, expModel.Target, expModel.Scope, expModel.ActionName)
	chaosBladeObj := convertExpModelToChaosBladeObject(uid, expModel)
	var err error
	resource, err := create(client, &chaosBladeObj)
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

func (e *Executor) checkCreateStatus(uid string, store cache.Store, client rest.Interface,
	resource *v1alpha1.ChaosBlade) *spec.Response {
	var chaosblade *v1alpha1.ChaosBlade
	item, _, err := store.GetByKey(resource.Name)
	if err != nil || item == nil {
		chaosblade, err = get(client, resource.Name)
	} else {
		chaosblade = item.(*v1alpha1.ChaosBlade)
	}
	logrus.Debugf("chaosblade: %+v", chaosblade)
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

func get(client rest.Interface, name string) (result *v1alpha1.ChaosBlade, err error) {
	result = &v1alpha1.ChaosBlade{}
	err = client.Get().
		Resource(ResourceName).
		Name(name).
		Do().
		Into(result)
	return
}

func create(client rest.Interface, chaosblade *v1alpha1.ChaosBlade) (result *v1alpha1.ChaosBlade, err error) {
	result = &v1alpha1.ChaosBlade{}
	err = client.Post().
		Resource(ResourceName).
		Body(chaosblade).
		Do().
		Into(result)
	return
}

func delete(client rest.Interface, name string, options *metav1.DeleteOptions) error {
	return client.Delete().
		Resource(ResourceName).
		Name(name).
		Body(options).
		Do().
		Error()
}

type ChaosBladeV1Alpha1Client struct {
	restClient rest.Interface
}

func newClient(kubeConfig string) (rest.Interface, error) {
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
	clusterConfig.NegotiatedSerializer = serializer.DirectCodecFactory{CodecFactory: scheme.Codecs}
	clusterConfig.UserAgent = rest.DefaultKubernetesUserAgent()

	client, err := rest.RESTClientFor(clusterConfig)
	if err != nil {
		return nil, err
	}
	bladeV1Alpha1Client := &ChaosBladeV1Alpha1Client{restClient: client}
	return bladeV1Alpha1Client.restClient, nil
}

func completed(operation string, statusResult StatusResult) bool {
	if operation == QueryDestroy {
		return statusResult.Success
	}
	statuses := statusResult.Statuses
	return statuses != nil && len(statuses) > 0
}
