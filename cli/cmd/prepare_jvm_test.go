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
	"testing"

	"github.com/spf13/cobra"
)

func TestPrepareJvmCommand_Run(t *testing.T) {
	jvmCommand := &PrepareJvmCommand{}
	jvmCommand.Init()
	jvmCommand.command.SetOutput(&bytes.Buffer{})
	jvmCommand.command.RunE = func(cmd *cobra.Command, args []string) error {
		return nil
	}
	jvmCommand.command.Execute()

	flag := jvmCommand.command.Flags().Lookup("process")
	if flag == nil {
		t.Errorf("unexpected error: %s", "no such flag --process")
	}
}
