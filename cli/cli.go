package main

import (
	"github.com/spf13/cobra"
	"os"
)

type Cli struct {
	rootCmd *cobra.Command
	Debug   bool
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
	flags.BoolVarP(&cli.Debug, "debug", "d", false, "Set client to DEBUG mode")
}

//Run command
func (cli *Cli) Run() error {
	return cli.rootCmd.Execute()
}
