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
)

func TestCli_Run(t *testing.T) {
	cli := NewCli()
	cli.rootCmd.SetOutput(&bytes.Buffer{})

	err := cli.Run()
	if err != nil {
		t.Errorf("unexpected error: %v", err)
	}

	flag := cli.rootCmd.Flags().Lookup("debug")
	if flag == nil {
		t.Errorf("unexpected error: %s", "no such flag --debug")
	}

	flag = cli.rootCmd.Flags().ShorthandLookup("d")
	if flag == nil {
		t.Errorf("unexpected error: %s", "no such shorthand flag -d")
	}
}
