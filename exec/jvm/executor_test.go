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

package jvm

import (
	"context"
	"encoding/json"
	"reflect"
	"testing"

	"github.com/chaosblade-io/chaosblade-spec-go/spec"
)

func TestExecutor_createUrl(t *testing.T) {
	ctx := context.WithValue(context.Background(), spec.Uid, "test-uid")
	model := &spec.ExpModel{
		Target:     "jvm",
		ActionName: "return",
		ActionFlags: map[string]string{
			"value":     "false",
			"classname": "com.example.Foo",
			"process":   "",
			"timeout":   "60",
		},
	}

	url, body, resp := NewExecutor().createUrl(ctx, "12345", model)
	if resp != nil {
		t.Fatalf("createUrl failed: %s", resp.Print())
	}

	wantUrl := "http://127.0.0.1:12345/" + DefaultUri + "/create"
	if url != wantUrl {
		t.Errorf("url = %s, want %s", url, wantUrl)
	}
	var got map[string]string
	if err := json.Unmarshal(body, &got); err != nil {
		t.Fatalf("unmarshal body %s: %v", body, err)
	}
	want := map[string]string{
		"target":    "jvm",
		"suid":      "test-uid",
		"action":    "return",
		"value":     "false",
		"classname": "com.example.Foo",
	}
	if !reflect.DeepEqual(got, want) {
		t.Errorf("body = %v, want %v", got, want)
	}
}
