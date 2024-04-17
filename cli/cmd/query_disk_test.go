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
	"bytes"
	"encoding/json"
	"fmt"
	"reflect"
	"testing"

	"github.com/chaosblade-io/chaosblade-spec-go/spec"
	"github.com/spf13/cobra"
)

func TestQueryDiskCommand_queryDiskInfo(t *testing.T) {
	command := &cobra.Command{}
	qdc := &QueryDiskCommand{}
	testQueryDiskDevice(t, command, qdc)

	testQueryDiskUnknownArg(t, command, qdc)
}

func testQueryDiskUnknownArg(t *testing.T, command *cobra.Command, qdc *QueryDiskCommand) {
	buffer := &bytes.Buffer{}
	command.SetOut(buffer)

	arg := "unknown"
	expectedErr := fmt.Errorf("the %s argument not found", arg)

	err := qdc.queryDiskInfo(command, arg)
	if err.Error() != expectedErr.Error() {
		t.Errorf("unexpected result: %s, expected: %s", err, expectedErr)
	}
}

func testQueryDiskDevice(t *testing.T, command *cobra.Command, qdc *QueryDiskCommand) {
	buffer := &bytes.Buffer{}
	command.SetOut(buffer)

	arg := "mount-point"
	err := qdc.queryDiskInfo(command, arg)
	if err != nil {
		t.Errorf("unexpected result: %s, expected no error", err)
	}
	// check print value
	var response spec.Response
	err = json.Unmarshal(buffer.Bytes(), &response)
	if err != nil {
		t.Errorf("unexpected result: %s, expected no error", err)
	}
	if !response.Success {
		t.Errorf("unexpected result: %t, expected: %t", response.Success, true)
	}

	// check response result
	inters := response.Result.([]interface{})
	if len(inters) <= 0 {
		t.Errorf("unexpected result: %d, expected greater zero", len(inters))
	}
	if _, ok := inters[0].(string); !ok {
		t.Errorf("unexpected result type: %s, expected string", reflect.TypeOf(inters[0]))
	}
}
