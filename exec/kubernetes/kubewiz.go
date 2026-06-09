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
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"strings"
	"time"

	"github.com/chaosblade-io/chaosblade-spec-go/log"
	"github.com/chaosblade-io/chaosblade-spec-go/spec"
)

// KubewizClient 封装与 kubewiz-core 的 HTTP 通信
type KubewizClient struct {
	baseURL     string
	clusterUUID string
	token       string
	httpClient  *http.Client
}

// kubewizResult 对应 kubewiz-core 的 Result<T> 响应格式
type kubewizResult struct {
	Code      int                    `json:"code"`
	Message   string                 `json:"message"`
	Data      map[string]interface{} `json:"data"`
	Timestamp int64                  `json:"timestamp"`
}

// kubewizTask 对应 kubewiz-core 的 Task 实体
type kubewizTask struct {
	TaskUUID     string   `json:"task_uuid"`
	Status       string   `json:"status"`
	Result       string   `json:"result"`
	ErrorMessage string   `json:"error_message"`
	ArtifactURLs []string `json:"artifact_urls"`
}

const (
	kubewizTaskCompleted = "COMPLETED"
	kubewizTaskFailed    = "FAILED"
	kubewizTaskCancelled = "CANCELLED"
)

// NewKubewizClient 创建 kubewiz 客户端实例
func NewKubewizClient(baseURL, clusterUUID, token string) *KubewizClient {
	return &KubewizClient{
		baseURL:     strings.TrimRight(baseURL, "/"),
		clusterUUID: clusterUUID,
		token:       token,
		httpClient: &http.Client{
			Timeout: 30 * time.Second,
		},
	}
}

// SubmitCreateTask 提交创建 ChaosBlade CR 的任务：仅执行 kubectl apply，不做状态轮询。
func (kc *KubewizClient) SubmitCreateTask(uid string, expModel *spec.ExpModel) (string, error) {
	crJSON, err := generateCRJSON(uid, expModel)
	if err != nil {
		return "", fmt.Errorf("generate CR json failed: %w", err)
	}

	// 用 sh -c + printf 管道创建 CR
	script := fmt.Sprintf("printf '%%s' '%s' | kubectl apply -f -",
		strings.ReplaceAll(crJSON, "'", `'\''`))

	command := map[string]interface{}{
		"args": []string{"sh", "-c", script},
	}

	target := map[string]interface{}{
		"type": "local",
		"name": "executor",
	}

	request := map[string]interface{}{
		"cluster_uuid": kc.clusterUUID,
		"target":       target,
		"command":      command,
		"job_desc":     fmt.Sprintf("chaosblade create %s %s %s", expModel.Scope, expModel.Target, expModel.ActionName),
	}

	task, err := kc.postTask(request)
	if err != nil {
		return "", err
	}
	return task.TaskUUID, nil
}

// SubmitQueryTask 提交查询 ChaosBlade CR 状态的任务
func (kc *KubewizClient) SubmitQueryTask(uid string) (string, error) {
	command := map[string]interface{}{
		"args": []string{"kubectl", "get", "chaosblade", uid, "-o", "json"},
	}

	target := map[string]interface{}{
		"type": "local",
		"name": "executor",
	}

	request := map[string]interface{}{
		"cluster_uuid": kc.clusterUUID,
		"target":       target,
		"command":      command,
		"job_desc":     fmt.Sprintf("chaosblade query %s", uid),
	}

	task, err := kc.postTask(request)
	if err != nil {
		return "", err
	}
	return task.TaskUUID, nil
}

// SubmitDestroyTask 提交删除 ChaosBlade CR 的任务
func (kc *KubewizClient) SubmitDestroyTask(uid string) (string, error) {
	command := map[string]interface{}{
		"args": []string{"kubectl", "delete", "chaosblade", uid, "--ignore-not-found"},
	}

	target := map[string]interface{}{
		"type": "local",
		"name": "executor",
	}

	request := map[string]interface{}{
		"cluster_uuid": kc.clusterUUID,
		"target":       target,
		"command":      command,
		"job_desc":     fmt.Sprintf("chaosblade destroy %s", uid),
	}

	task, err := kc.postTask(request)
	if err != nil {
		return "", err
	}
	return task.TaskUUID, nil
}

// PollTaskUntilDone 轮询 kubewiz task 直到完成或超时
func (kc *KubewizClient) PollTaskUntilDone(ctx context.Context, taskUUID string, interval time.Duration) (*kubewizTask, error) {
	ticker := time.NewTicker(interval)
	defer ticker.Stop()

	for {
		select {
		case <-ctx.Done():
			return nil, fmt.Errorf("poll timeout for task %s", taskUUID)
		case <-ticker.C:
			task, err := kc.getTask(ctx, taskUUID)
			if err != nil {
				errMsg := err.Error()
				// 不可重试错误（如 task 不存在）直接中止
				if strings.Contains(errMsg, "HTTP 404") || strings.Contains(errMsg, "not found") {
					return nil, fmt.Errorf("task %s not found: %w", taskUUID, err)
				}
				log.Warnf(ctx, "poll task %s failed: %v", taskUUID, err)
				continue
			}
			if task.isTerminal() {
				return task, nil
			}
		}
	}
}

func (t *kubewizTask) isTerminal() bool {
	return t.Status == kubewizTaskCompleted ||
		t.Status == kubewizTaskFailed ||
		t.Status == kubewizTaskCancelled
}

// GetArtifact 获取任务执行产物（命令输出）的内容
func (kc *KubewizClient) GetArtifact(ctx context.Context, artifactURL string) (string, error) {
	endpoint := fmt.Sprintf("%s/api/v1/tasks/artifact?artifact_url=%s",
		kc.baseURL, url.QueryEscape(artifactURL))
	req, err := http.NewRequestWithContext(ctx, "GET", endpoint, nil)
	if err != nil {
		return "", fmt.Errorf("create artifact request failed: %w", err)
	}
	kc.setAuthHeader(req)

	resp, err := kc.httpClient.Do(req)
	if err != nil {
		return "", fmt.Errorf("get artifact failed: %w", err)
	}
	defer resp.Body.Close()

	data, err := io.ReadAll(resp.Body)
	if err != nil {
		return "", fmt.Errorf("read artifact body failed: %w", err)
	}
	if resp.StatusCode != http.StatusOK {
		return "", fmt.Errorf("get artifact failed [HTTP %d]: %s", resp.StatusCode, string(data))
	}
	return string(data), nil
}

// postTask 向 kubewiz-core 提交任务
func (kc *KubewizClient) postTask(request map[string]interface{}) (*kubewizTask, error) {
	body, err := json.Marshal(request)
	if err != nil {
		return nil, fmt.Errorf("marshal request failed: %w", err)
	}

	req, err := http.NewRequest("POST", kc.baseURL+"/api/v1/tasks", bytes.NewReader(body))
	if err != nil {
		return nil, fmt.Errorf("create request failed: %w", err)
	}
	req.Header.Set("Content-Type", "application/json")
	kc.setAuthHeader(req)

	resp, err := kc.httpClient.Do(req)
	if err != nil {
		return nil, fmt.Errorf("submit task failed: %w", err)
	}
	defer resp.Body.Close()

	data, err := io.ReadAll(resp.Body)
	if err != nil {
		return nil, fmt.Errorf("read response failed: %w", err)
	}
	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("kubewiz request failed [HTTP %d]: %s", resp.StatusCode, string(data))
	}

	var result kubewizResult
	if err := json.Unmarshal(data, &result); err != nil {
		return nil, fmt.Errorf("parse response failed: %w", err)
	}
	if result.Code != 200 {
		return nil, fmt.Errorf("kubewiz error [%d]: %s", result.Code, result.Message)
	}

	task := extractTask(result.Data)
	if task == nil {
		return nil, fmt.Errorf("failed to extract task from response")
	}
	return task, nil
}

// getTask 查询 kubewiz task 状态
func (kc *KubewizClient) getTask(ctx context.Context, taskUUID string) (*kubewizTask, error) {
	req, err := http.NewRequestWithContext(ctx, "GET", kc.baseURL+"/api/v1/tasks/"+taskUUID, nil)
	if err != nil {
		return nil, err
	}
	kc.setAuthHeader(req)

	resp, err := kc.httpClient.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()

	data, err := io.ReadAll(resp.Body)
	if err != nil {
		return nil, err
	}
	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("get task failed [HTTP %d]: %s", resp.StatusCode, string(data))
	}

	var result kubewizResult
	if err := json.Unmarshal(data, &result); err != nil {
		return nil, fmt.Errorf("parse response failed: %w", err)
	}
	if result.Code != 200 {
		return nil, fmt.Errorf("get task failed [%d]: %s", result.Code, result.Message)
	}

	task := extractTask(result.Data)
	if task == nil {
		return nil, fmt.Errorf("failed to extract task from response")
	}
	return task, nil
}

// setAuthHeader 根据 token 格式自动选择认证方式：
// - JWT token（以 eyJ 开头）使用 Authorization: Bearer <token>
// - User token（普通字符串）使用 X-User-Token: <token>
func (kc *KubewizClient) setAuthHeader(req *http.Request) {
	if len(kc.token) > 3 && kc.token[:3] == "eyJ" {
		req.Header.Set("Authorization", "Bearer "+kc.token)
	} else {
		req.Header.Set("X-User-Token", kc.token)
	}
}

// extractTask 从 kubewiz 响应的 data map 中提取 task 信息，
// 并从 result JSON 字符串中解析出 artifact_urls。
func extractTask(data map[string]interface{}) *kubewizTask {
	if data == nil {
		return nil
	}

	task := &kubewizTask{}

	if v, ok := data["task_uuid"]; ok {
		task.TaskUUID, _ = v.(string)
	}
	if task.TaskUUID == "" {
		if v, ok := data["uuid"]; ok {
			task.TaskUUID, _ = v.(string)
		}
	}
	if v, ok := data["status"]; ok {
		task.Status, _ = v.(string)
	}
	if v, ok := data["result"]; ok {
		task.Result, _ = v.(string)
	}
	if v, ok := data["error_message"]; ok {
		task.ErrorMessage, _ = v.(string)
	}

	task.ArtifactURLs = parseArtifactURLs(task.Result)

	// 校验 TaskUUID 非空，避免返回无效 task
	if task.TaskUUID == "" {
		return nil
	}

	return task
}

// parseArtifactURLs 从 task.result JSON 字符串中解析 artifact_urls 数组
func parseArtifactURLs(resultJSON string) []string {
	if resultJSON == "" {
		return nil
	}
	var payload struct {
		ArtifactURLs []string `json:"artifact_urls"`
	}
	if err := json.Unmarshal([]byte(resultJSON), &payload); err != nil {
		return nil
	}
	return payload.ArtifactURLs
}

// ConvertTaskToResponse 将 kubewiz 任务结果转换为 ChaosBlade spec.Response
func (kc *KubewizClient) ConvertTaskToResponse(ctx context.Context, task *kubewizTask, uid string, operation string) *spec.Response {
	if task == nil {
		return spec.ResponseFailWithFlags(spec.K8sExecFailed, "kubewiz", "task is nil")
	}

	switch task.Status {
	case kubewizTaskCompleted:
		if len(task.ArtifactURLs) == 0 {
			return spec.ReturnSuccess(uid)
		}
		output, err := kc.GetArtifact(ctx, task.ArtifactURLs[0])
		if err != nil {
			return spec.ReturnSuccess(uid)
		}
		return parseChaosBladeOutput(output, uid, operation)
	case kubewizTaskFailed:
		msg := ""
		// 优先从 artifact 获取实际执行错误
		if len(task.ArtifactURLs) > 0 {
			if output, err := kc.GetArtifact(ctx, task.ArtifactURLs[0]); err == nil && strings.TrimSpace(output) != "" {
				msg = strings.TrimSpace(output)
			}
		}
		if msg == "" {
			msg = task.ErrorMessage
		}
		if msg == "" {
			msg = "kubewiz task failed"
		}
		return spec.ResponseFailWithFlags(spec.K8sExecFailed, "kubewiz", msg)
	case kubewizTaskCancelled:
		return spec.ResponseFailWithFlags(spec.K8sExecFailed, "kubewiz", "task cancelled")
	default:
		return spec.ResponseFailWithFlags(spec.K8sExecFailed, "kubewiz", "unexpected status: "+task.Status)
	}
}

// parseChaosBladeOutput 尝试解析 kubectl get chaosblade -o json 的输出，
// 如果解析失败（比如 destroy 场景的简单文本输出），按成功处理。
func parseChaosBladeOutput(output string, uid string, operation string) *spec.Response {
	trimmed := strings.TrimSpace(output)
	if trimmed == "" {
		return spec.ReturnSuccess(uid)
	}
	// 不是 JSON 对象 → 当作成功（destroy 场景的纯文本）
	if !strings.HasPrefix(trimmed, "{") {
		return spec.ReturnSuccess(uid)
	}

	var cr struct {
		Status struct {
			Phase       string `json:"phase"`
			ExpStatuses []struct {
				State       string                   `json:"state"`
				Success     bool                     `json:"success"`
				Error       string                   `json:"error"`
				ResStatuses []map[string]interface{} `json:"resStatuses"`
			} `json:"expStatuses"`
		} `json:"status"`
	}
	if err := json.Unmarshal([]byte(trimmed), &cr); err != nil {
		return spec.ReturnSuccess(uid)
	}

	phase := cr.Status.Phase
	statuses := make([]map[string]interface{}, 0)
	if len(cr.Status.ExpStatuses) > 0 {
		exp := cr.Status.ExpStatuses[0]
		if len(exp.ResStatuses) > 0 {
			statuses = exp.ResStatuses
		} else {
			statuses = append(statuses, map[string]interface{}{
				"state":   exp.State,
				"success": exp.Success,
				"error":   exp.Error,
			})
		}
	}

	result := map[string]interface{}{
		"uid":      uid,
		"phase":    phase,
		"success":  true,
		"error":    "",
		"statuses": statuses,
	}

	switch phase {
	case "Running":
		if operation == "destroy" {
			result["success"] = false
			result["error"] = fmt.Sprintf("expected Destroyed but got %s", phase)
			return spec.ResponseFailWithResult(spec.UnexpectedStatus, result, "destroyed", phase)
		}
		return spec.ReturnSuccess(result)
	case "Destroyed":
		if operation == "create" {
			result["success"] = false
			result["error"] = fmt.Sprintf("expected Running but got %s", phase)
			return spec.ResponseFailWithResult(spec.UnexpectedStatus, result, "running", phase)
		}
		return spec.ReturnSuccess(result)
	case "Error":
		errMsg := ""
		if len(cr.Status.ExpStatuses) > 0 {
			errMsg = cr.Status.ExpStatuses[0].Error
		}
		if errMsg == "" {
			errMsg = "chaosblade phase: Error"
		}
		result["success"] = false
		result["error"] = errMsg
		return spec.ResponseFailWithResult(spec.K8sExecFailed, result, "kubewiz", errMsg)
	default:
		// 未知 phase（如 Initial/空）→ 当作成功，由上层等待逻辑继续轮询
		return spec.ReturnSuccess(result)
	}
}

// QueryStatusViaKubewiz 通过 kubewiz 通道查询 ChaosBlade CR 状态
func QueryStatusViaKubewiz(ctx context.Context, operation, kubewizURL, clusterUUID, kubewizToken, uid string) *spec.Response {
	kc := NewKubewizClient(kubewizURL, clusterUUID, kubewizToken)

	taskUUID, err := kc.SubmitQueryTask(uid)
	if err != nil {
		return spec.ResponseFailWithFlags(spec.K8sExecFailed, "kubewiz-submit", err)
	}

	pollCtx, cancel := context.WithTimeout(ctx, 20*time.Second)
	defer cancel()
	task, pollErr := kc.PollTaskUntilDone(pollCtx, taskUUID, 2*time.Second)
	if pollErr != nil {
		return spec.ResponseFailWithFlags(spec.K8sExecFailed, "kubewiz-timeout", pollErr)
	}

	return kc.ConvertTaskToResponse(ctx, task, uid, operation)
}

// generateCRJSON 将 ExpModel 转换成 ChaosBlade CR 的紧凑 JSON 字符串
func generateCRJSON(uid string, expModel *spec.ExpModel) (string, error) {
	matchers := buildMatchers(expModel.ActionFlags)

	cr := map[string]interface{}{
		"apiVersion": "chaosblade.io/v1alpha1",
		"kind":       "ChaosBlade",
		"metadata": map[string]interface{}{
			"name": uid,
		},
		"spec": map[string]interface{}{
			"experiments": []map[string]interface{}{
				{
					"scope":    expModel.Scope,
					"target":   expModel.Target,
					"action":   expModel.ActionName,
					"desc":     "created by blade command",
					"matchers": matchers,
				},
			},
		},
	}

	data, err := json.Marshal(cr)
	if err != nil {
		return "", err
	}
	return string(data), nil
}

// buildMatchers 将 ActionFlags 转换为 ChaosBlade CR matchers，
// 跳过 K8s 连接相关的 flags，values 按逗号分割成数组。
func buildMatchers(flags map[string]string) []map[string]interface{} {
	skip := map[string]bool{
		KubeConfigFlag.Name:   true,
		WaitingTimeFlag.Name:  true,
		KubectlProxyFlag.Name: true,
		TokenFlag.Name:        true,
		KubewizURLFlag.Name:   true,
		ClusterUUIDFlag.Name:  true,
		KubewizTokenFlag.Name: true,
	}
	matchers := make([]map[string]interface{}, 0, len(flags))
	for name, values := range flags {
		if skip[name] || values == "" {
			continue
		}
		matchers = append(matchers, map[string]interface{}{
			"name":  name,
			"value": strings.Split(values, ","),
		})
	}
	return matchers
}
