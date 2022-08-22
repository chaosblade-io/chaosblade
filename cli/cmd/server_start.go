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
	"context"
	"crypto/tls"
	"crypto/x509"
	"fmt"
	"github.com/chaosblade-io/chaosblade-spec-go/log"
	"io/ioutil"
	"net/http"
	"os"
	"path"
	"time"

	"github.com/chaosblade-io/chaosblade-spec-go/channel"
	"github.com/chaosblade-io/chaosblade-spec-go/spec"
	"github.com/chaosblade-io/chaosblade-spec-go/util"
	"github.com/spf13/cobra"
)

const startServerKey = "blade server start --nohup"

type StartServerCommand struct {
	baseCommand
	ip       string
	port     string
	nohup    bool
	mtls     bool
	cafile   string
	certfile string
	keyfile  string
}

func (ssc *StartServerCommand) Init() {
	ssc.command = &cobra.Command{
		Use:     "start",
		Short:   "Start server mode, exposes web services",
		Long:    "Start server mode, exposes web services. Under the mode, you can send http or https request to trigger experiments",
		Aliases: []string{"s"},
		RunE: func(cmd *cobra.Command, args []string) error {
			return ssc.run(cmd, args)
		},
		Example: startServerExample(),
	}
	ssc.command.Flags().StringVarP(&ssc.ip, "ip", "i", "", "service ip address, default value is *")
	ssc.command.Flags().StringVarP(&ssc.port, "port", "p", "9526", "service port")
	ssc.command.Flags().BoolVarP(&ssc.nohup, "nohup", "n", false, "used by internal")
	ssc.command.Flags().BoolVarP(&ssc.mtls, "mtls", "", false, "use mtls authentication, default value is false")
	ssc.command.Flags().StringVarP(&ssc.cafile, "ca-file", "", "", "TLS CA file")
	ssc.command.Flags().StringVarP(&ssc.certfile, "cert-file", "", "", "server TLS cert file")
	ssc.command.Flags().StringVarP(&ssc.keyfile, "key-file", "", "", "server TLS key file")
}

func (ssc *StartServerCommand) run(cmd *cobra.Command, args []string) error {
	// check if the mtls parameters are correct
	if ssc.mtls && ssc.cafile == "" || ssc.certfile == "" || ssc.keyfile == "" {
		return spec.ResponseFailWithFlags(spec.OsCmdExecFailed, startServerKey,
			"start blade server failed, mtls needs ca, cert and key file")
	}
	if !util.IsExist(ssc.cafile) {
		return spec.ResponseFailWithFlags(spec.OsCmdExecFailed, startServerKey,
			"start blade server failed, ca file does not exist")
	}
	if !util.IsExist(ssc.certfile) {
		return spec.ResponseFailWithFlags(spec.OsCmdExecFailed, startServerKey,
			"start blade server failed, cert file does not exist")
	}
	if !util.IsExist(ssc.keyfile) {
		return spec.ResponseFailWithFlags(spec.OsCmdExecFailed, startServerKey,
			"start blade server failed, key file does not exist")
	}
	// check if the process named `blade server --start` exists or not
	pids, err := channel.NewLocalChannel().GetPidsByProcessName(startServerKey, context.TODO())
	if err != nil {
		return spec.ResponseFailWithFlags(spec.OsCmdExecFailed, startServerKey, err)
	}
	if len(pids) > 0 {
		return spec.ResponseFailWithFlags(spec.ChaosbladeServerStarted)
	}
	if ssc.nohup {
		ssc.start0()
	}
	err = ssc.start()
	if err != nil {
		return err
	}
	cmd.Println(fmt.Sprintf("success, listening on %s:%s", ssc.ip, ssc.port))
	return nil
}

// start used nohup command and check the process
func (ssc *StartServerCommand) start() error {
	// use nohup to invoke blade server start command
	cl := channel.NewLocalChannel()
	bladeBin := path.Join(util.GetProgramPath(), "blade")
	args := fmt.Sprintf("%s server start --nohup --port %s", bladeBin, ssc.port)
	if ssc.ip != "" {
		args = fmt.Sprintf("%s --ip %s", args, ssc.ip)
	}
	if ssc.mtls {
		args = fmt.Sprintf("%s --mtls --ca-file %s --cert-file %s --key-file %s", args, ssc.cafile, ssc.certfile, ssc.keyfile)
	}
	ctx := context.Background()
	response := cl.Run(ctx, "nohup", fmt.Sprintf("%s > /dev/null 2>&1 &", args))
	if !response.Success {
		return response
	}
	time.Sleep(time.Second)
	// check process
	pids, err := channel.NewLocalChannel().GetPidsByProcessName(startServerKey, context.TODO())
	if err != nil {
		return spec.ResponseFailWithFlags(spec.OsCmdExecFailed, startServerKey, err)
	}
	if len(pids) == 0 {
		// read logs
		logFile, err := util.GetLogFile(util.Blade)
		if err != nil {
			return spec.ResponseFailWithFlags(spec.OsCmdExecFailed, startServerKey,
				"start blade server failed and can't get log file")
		}
		if !util.IsExist(logFile) {
			return spec.ResponseFailWithFlags(spec.OsCmdExecFailed, startServerKey,
				"start blade server failed and log file does not exist")
		}
		response := cl.Run(context.TODO(), "tail", fmt.Sprintf("-1 %s", logFile))
		if !response.Success {
			return spec.ResponseFailWithFlags(spec.OsCmdExecFailed, startServerKey,
				"start blade server failed and can't read log file")
		}
		return spec.ResponseFailWithFlags(spec.OsCmdExecFailed, startServerKey, response.Err)
	}
	log.Infof(ctx, "start blade server success, listen on %s:%s", ssc.ip, ssc.port)
	return nil
}

// start0 starts web service
func (ssc *StartServerCommand) start0() {
	if ssc.mtls {
		go func() {
			pool := x509.NewCertPool()
			caCrt, _ := ioutil.ReadFile(ssc.cafile)
			pool.AppendCertsFromPEM(caCrt)
			s := &http.Server{
				Addr:    ssc.ip + ":" + ssc.port,
				Handler: nil,
				TLSConfig: &tls.Config{
					ClientCAs:  pool,
					ClientAuth: tls.RequireAndVerifyClientCert,
				},
			}
			err := s.ListenAndServeTLS(ssc.certfile, ssc.keyfile)
			if err != nil {
				log.Errorf(context.Background(), "start blade server error, %v", err)
				//log.Error(err, "start blade server error")
				os.Exit(1)
			}
		}()
	} else {
		go func() {
			err := http.ListenAndServe(ssc.ip+":"+ssc.port, nil)
			if err != nil {
				log.Errorf(context.Background(), "start blade server error, %v", err)
				//log.Error(err, "start blade server error")
				os.Exit(1)
			}
		}()
	}
	Register("/chaosblade")
	util.Hold()
}

func Register(requestPath string) {
	http.HandleFunc(requestPath, func(writer http.ResponseWriter, request *http.Request) {
		err := request.ParseForm()
		if err != nil {
			fmt.Fprintf(writer, spec.ReturnFail(spec.ParameterRequestFailed, err.Error()).Print())
			return
		}
		cmds := request.Form["cmd"]
		if len(cmds) != 1 {
			fmt.Fprintf(writer, spec.ResponseFailWithFlags(spec.ParameterLess, "cmd").Print())
			return
		}
		ctx := context.WithValue(context.Background(), "mode", "server")
		response := channel.NewLocalChannel().Run(ctx, path.Join(util.GetProgramPath(), "blade"), cmds[0])
		log.Debugf(ctx, "Server response: %v", response)
		fmt.Fprintf(writer, response.Print())
	})
}

func startServerExample() string {
	return `blade server start --port 8000`
}
