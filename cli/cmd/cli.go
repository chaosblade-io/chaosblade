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
	"os"

	"github.com/chaosblade-io/chaosblade-spec-go/util"
	"github.com/spf13/cobra"
)

type Cli struct {
	rootCmd *cobra.Command
}

//NewCli returns the cli instance used to register and execute command
func NewCli() *Cli {
	cli := &Cli{
		rootCmd: &cobra.Command{
			Use:   "blade",
			Short: "An easy to use and powerful chaos toolkit",
			Long:  "An easy to use and powerful chaos engineering experiment toolkit",
		},
	}
	cli.rootCmd.SetOutput(os.Stdout)
	cli.setFlags()
	return cli
}

// setFlags defines flags for root command
func (cli *Cli) setFlags() {
	flags := cli.rootCmd.PersistentFlags()
	flags.BoolVarP(&util.Debug, "debug", "d", false, "Set client to DEBUG mode")
	//flags.StringVarP(&util.LogLevel, "log-level", "l", "info", "level of logging wanted. 1=DEBUG, 0=INFO, -1=WARN, A higher verbosity level means a log message is less important.")
}

//Run command
func (cli *Cli) Run() error {
	return cli.rootCmd.Execute()
}
