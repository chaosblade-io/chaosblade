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

package v1alpha1

import (
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
)

type ClusterPhase string

const (
	ClusterPhaseInitial     ClusterPhase = ""
	ClusterPhaseInitialized ClusterPhase = "Initialized"
	ClusterPhaseRunning     ClusterPhase = "Running"
	ClusterPhaseUpdating    ClusterPhase = "Updating"
	ClusterPhaseDestroying  ClusterPhase = "Destroying"
	ClusterPhaseDestroyed   ClusterPhase = "Destroyed"
	ClusterPhaseError       ClusterPhase = "Error"
)

type ChaosBladeSpec struct {
	Experiments []ExperimentSpec `json:"experiments"`
}

type ExperimentSpec struct {
	Scope    string     `json:"scope"`
	Target   string     `json:"target"`
	Action   string     `json:"action"`
	Desc     string     `json:"desc,omitempty"`
	Matchers []FlagSpec `json:"matchers,omitempty"`
}

type FlagSpec struct {
	Name  string   `json:"name"`
	Value []string `json:"value"`
}

type ChaosBladeStatus struct {
	Phase       ClusterPhase       `json:"phase,omitempty"`
	ExpStatuses []ExperimentStatus `json:"expStatuses"`
}

func (in *ResourceStatus) CreateFailResourceStatus(err string, code int32) ResourceStatus {
	in.State = ErrorState
	in.Error = err
	in.Success = false
	in.Code = code
	return *in
}

func (in *ResourceStatus) CreateSuccessResourceStatus() ResourceStatus {
	in.State = SuccessState
	in.Success = true
	return *in
}

const (
	PodKind       = "pod"
	ContainerKind = "container"
	NodeKind      = "node"
)

type ResourceStatus struct {
	Id         string `json:"id,omitempty"`
	State      string `json:"state"`
	Code       int32  `json:"code,omitempty"`
	Error      string `json:"error,omitempty"`
	Success    bool   `json:"success"`
	Kind       string `json:"kind"`
	Identifier string `json:"identifier,omitempty"`
}

const (
	SuccessState   = "Success"
	ErrorState     = "Error"
	DestroyedState = "Destroyed"
)

func CreateFailExperimentStatus(err string, resStatuses []ResourceStatus) ExperimentStatus {
	return ExperimentStatus{Success: false, State: ErrorState, Error: err, ResStatuses: resStatuses}
}

func CreateSuccessExperimentStatus(resStatuses []ResourceStatus) ExperimentStatus {
	return ExperimentStatus{Success: true, State: SuccessState, ResStatuses: resStatuses}
}

func CreateDestroyedExperimentStatus(resStatuses []ResourceStatus) ExperimentStatus {
	return ExperimentStatus{Success: true, State: DestroyedState, ResStatuses: resStatuses}
}

func CreateFailResStatuses(code int32, err, uid string) []ResourceStatus {
	return []ResourceStatus{{
		Error:   err,
		Code:    code,
		Id:      uid,
		Success: false,
	}}
}

type ExperimentStatus struct {
	Scope       string           `json:"scope"`
	Target      string           `json:"target"`
	Action      string           `json:"action"`
	Success     bool             `json:"success"`
	State       string           `json:"state"`
	Error       string           `json:"error,omitempty"`
	ResStatuses []ResourceStatus `json:"resStatuses,omitempty"`
}

type ChaosBlade struct {
	metav1.TypeMeta   `json:",inline"`
	metav1.ObjectMeta `json:"metadata,omitempty"`

	Spec   ChaosBladeSpec   `json:"spec,omitempty"`
	Status ChaosBladeStatus `json:"status,omitempty"`
}

type ChaosBladeList struct {
	metav1.TypeMeta `json:",inline"`
	metav1.ListMeta `json:"metadata,omitempty"`
	Items           []ChaosBlade `json:"items"`
}

func init() {
	SchemeBuilder.Register(&ChaosBlade{}, &ChaosBladeList{})
}
