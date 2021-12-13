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
	"github.com/chaosblade-io/chaosblade/data"
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
	flags.StringVar(&data.Type, "db-type", "sqlite3", "Use specific db type to store experiment data, support: mysql/sqlite3")
	flags.StringVar(&data.Host, "db-host", "127.0.0.1", "If remote db server like mysql used for db-type, set the host of the db server")
	flags.IntVar(&data.Port, "db-port", 3306, "If remote db server like mysql used for db-type, set the port of the db server")
	flags.StringVar(&data.Database, "db-name", "chaosblade", "If remote db server like mysql used for db-type, set the target db name of the db server")
	flags.StringVar(&data.Username, "db-user", "root", "If remote db server like mysql used for db-type, set the username for db connection")
	flags.StringVar(&data.Password, "db-pwd", "", "If remote db server like mysql used for db-type, set the password for db connection (default \"\")")
	flags.IntVar(&data.Timeout, "db-timeout", 60, "If remote db server like mysql used for db-type, set the timeout for db connection")
	flags.StringVar(&data.DatPath, "dat-path", util.GetProgramPath(), "If default or local db like sqlite3 used for db-type, set the directory path to save chaosblade.dat file")
	flags.StringVar(&util.LogPath, "log-path", util.GetProgramPath(), "Use log-path to set custom path for saving chaosblade logs")
}

//Run command
func (cli *Cli) Run() error {
	return cli.rootCmd.Execute()
}
