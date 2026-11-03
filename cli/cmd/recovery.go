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

package cmd

import (
	"context"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"net/http"
	"os"
	"os/signal"
	"path/filepath"
	"syscall"
	"time"

	"github.com/spf13/cobra"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime/schema"
	"k8s.io/client-go/dynamic"
	"k8s.io/client-go/tools/clientcmd"

	"github.com/chaosblade-io/chaosblade-spec-go/spec"
	"github.com/chaosblade-io/chaosblade-spec-go/util"
	"github.com/chaosblade-io/chaosblade/recovery"
)

type RecoveryCommand struct{ baseCommand }

func (*RecoveryCommand) localManager() (*recovery.Manager, error) {
	return recovery.Open(os.Getenv("CHAOSBLADE_RECOVERY_DIR"), os.Getenv("CHAOSBLADE_CLUSTER_ID"), os.Getenv("KUBERNETES_NODENAME"), recovery.NewNetworkBackend(util.GetProgramPath()))
}

func (rc *RecoveryCommand) Init() {
	rc.command = &cobra.Command{Use: "recovery", Short: "Inspect or stop registered experiments without the Kubernetes API"}
	for _, op := range []string{"register", "cancel", "apply", "local-status", "local-stop"} {
		local := &cobra.Command{Use: op, Hidden: true, Args: cobra.RangeArgs(1, 2)}
		local.RunE = func(cmd *cobra.Command, args []string) error {
			ctx, cancel := context.WithTimeout(cmd.Context(), 45*time.Second)
			defer cancel()
			m, err := rc.localManager()
			if err != nil {
				return err
			}
			defer m.Close()
			var result interface{}
			switch op {
			case "register", "cancel":
				if len(args) != 1 {
					return fmt.Errorf("register requires one encoded plan")
				}
				if len(args[0]) > 350000 {
					return fmt.Errorf("plan exceeds size limit")
				}
				b, e := base64.RawURLEncoding.DecodeString(args[0])
				if e != nil {
					return e
				}
				var plan recovery.Plan
				if e = json.Unmarshal(b, &plan); e != nil {
					return e
				}
				if op == "cancel" {
					result, err = m.Cancel(ctx, plan)
				} else {
					result, err = m.Register(ctx, plan)
				}
			case "apply":
				if len(args) != 2 {
					return fmt.Errorf("apply requires run ID and target ID")
				}
				result, err = m.Apply(ctx, args[0], args[1])
			case "local-status":
				result, err = m.Status(ctx, args[0])
			case "local-stop":
				result, err = m.Stop(ctx, args[0])
			}
			if err != nil {
				return err
			}
			return json.NewEncoder(cmd.OutOrStdout()).Encode(result)
		}
		rc.command.AddCommand(local)
	}
	var files recovery.TLSFiles
	var listen string
	serve := &cobra.Command{Use: "serve", Short: "Run the constrained node recovery service", Args: cobra.NoArgs, RunE: func(cmd *cobra.Command, args []string) error {
		config, err := recovery.TLSConfig(files, true)
		if err != nil {
			return err
		}
		m, err := rc.localManager()
		if err != nil {
			return err
		}
		defer m.Close()
		server := &http.Server{Addr: listen, Handler: m.Handler(), TLSConfig: config, ReadHeaderTimeout: 5 * time.Second, ReadTimeout: 10 * time.Second, WriteTimeout: 55 * time.Second, IdleTimeout: 30 * time.Second, MaxHeaderBytes: 8192}
		ctx, stop := signal.NotifyContext(cmd.Context(), syscall.SIGINT, syscall.SIGTERM)
		defer stop()
		done := make(chan struct{})
		defer close(done)
		go func() {
			select {
			case <-ctx.Done():
				shutdown, cancel := context.WithTimeout(context.Background(), 50*time.Second)
				defer cancel()
				_ = server.Shutdown(shutdown)
			case <-done:
			}
		}()
		err = server.ListenAndServeTLS("", "")
		if err == http.ErrServerClosed {
			return nil
		}
		return err
	}}
	serve.Flags().StringVar(&listen, "listen", ":9444", "Node address and port to bind")
	tlsFlags(serve, &files, true)
	rc.command.AddCommand(serve)
	for _, op := range []string{"status", "stop"} {
		var planFile, profileFile, experiment, runID string
		var credentials recovery.TLSFiles
		remote := &cobra.Command{Use: op, Short: op + " an entire registered execution using its saved plan", Args: cobra.NoArgs, RunE: func(cmd *cobra.Command, args []string) error {
			var plan recovery.Plan
			var profile recovery.Profile
			if profileFile != "" {
				data, err := os.ReadFile(profileFile)
				if err != nil {
					return err
				}
				if err = json.Unmarshal(data, &profile); err != nil {
					return err
				}
				credentials = profile.TLS
			}
			tls, err := recovery.TLSConfig(credentials, false)
			if err != nil {
				return err
			}
			client := &recovery.Client{TLS: tls}
			if planFile != "" {
				data, err := os.ReadFile(planFile)
				if err != nil {
					return err
				}
				if err = json.Unmarshal(data, &plan); err != nil {
					return err
				}
			} else {
				if profileFile == "" {
					return fmt.Errorf("--plan or --profile is required")
				}
				plan, err = client.Discover(cmd.Context(), profile, experiment, runID)
				if err != nil {
					return err
				}
			}
			if profileFile != "" && profile.ClusterID != plan.ClusterID {
				return fmt.Errorf("plan and profile belong to different clusters")
			}
			result, err := client.Run(cmd.Context(), plan, op == "stop")
			if e := json.NewEncoder(cmd.OutOrStdout()).Encode(result); e != nil {
				return e
			}
			return err
		}}
		remote.Flags().StringVar(&planFile, "plan", "", "Saved immutable recovery plan (no kubeconfig required)")
		remote.Flags().StringVar(&profileFile, "profile", "", "Recovery cluster profile with seed endpoints and TLS files")
		remote.Flags().StringVar(&experiment, "experiment", "", "Experiment name to discover through the recovery service")
		remote.Flags().StringVar(&runID, "run", "", "Exact run ID when an experiment name was reused")
		tlsFlags(remote, &credentials, false)
		rc.command.AddCommand(remote)
	}
	var kubeconfig, output string
	export := &cobra.Command{Use: "export EXPERIMENT", Short: "Save the recovery plan while the Kubernetes API is available", Args: cobra.ExactArgs(1), RunE: func(cmd *cobra.Command, args []string) error {
		loading := clientcmd.NewDefaultClientConfigLoadingRules()
		loading.ExplicitPath = kubeconfig
		config, err := clientcmd.NewNonInteractiveDeferredLoadingClientConfig(loading, &clientcmd.ConfigOverrides{}).ClientConfig()
		if err != nil {
			return err
		}
		config.Timeout = 15 * time.Second
		client, err := dynamic.NewForConfig(config)
		if err != nil {
			return err
		}
		obj, err := client.Resource(schema.GroupVersionResource{Group: "chaosblade.io", Version: "v1alpha1", Resource: "chaosblades"}).Get(cmd.Context(), args[0], metav1.GetOptions{})
		if err != nil {
			return err
		}
		b, err := json.Marshal(obj.Object["status"])
		if err != nil {
			return err
		}
		var status struct {
			Recovery struct {
				Plan *recovery.Plan `json:"plan"`
			} `json:"recovery"`
		}
		if err = json.Unmarshal(b, &status); err != nil {
			return err
		}
		if status.Recovery.Plan == nil {
			return fmt.Errorf("experiment has no registered recovery plan yet")
		}
		if err = recovery.ValidatePlan(*status.Recovery.Plan); err != nil {
			return err
		}
		b, err = json.MarshalIndent(status.Recovery.Plan, "", "  ")
		if err != nil {
			return err
		}
		// Avoid silently replacing another execution's receipt.
		f, err := os.OpenFile(filepath.Clean(output), os.O_WRONLY|os.O_CREATE|os.O_EXCL, 0o600)
		if err != nil {
			return err
		}
		_, err = f.Write(append(b, '\n'))
		closeErr := f.Close()
		if err != nil {
			return err
		}
		return closeErr
	}}
	export.Flags().StringVar(&kubeconfig, "kubeconfig", "", "Kubeconfig path (defaults to standard loading rules)")
	export.Flags().StringVar(&output, "output", "", "New file to save the immutable plan")
	_ = export.MarkFlagRequired("output")
	rc.command.AddCommand(export)
}

func tlsFlags(cmd *cobra.Command, f *recovery.TLSFiles, required bool) {
	cmd.Flags().StringVar(&f.CA, "ca", "", "Dedicated recovery CA file")
	cmd.Flags().StringVar(&f.Certificate, "cert", "", "Recovery certificate file")
	cmd.Flags().StringVar(&f.Key, "key", "", "Recovery private key file")
	if required {
		for _, flag := range []string{"ca", "cert", "key"} {
			_ = cmd.MarkFlagRequired(flag)
		}
	}
}

// Legacy commands share the same local effect lock. While a managed execution
// owns an interface, legacy network commands are conservatively refused on this
// Tool node, preventing their broad tc inverse from deleting a managed effect.
func legacyNetworkGuard(model *spec.ExpModel) (func(), error) {
	if model.Target != "network" || os.Getenv("CHAOSBLADE_RECOVERY_DIR") == "" {
		return func() {}, nil
	}
	m, err := (&RecoveryCommand{}).localManager()
	if err != nil {
		return nil, err
	}
	ctx, cancel := context.WithTimeout(context.Background(), 45*time.Second)
	unlock, err := m.GuardLegacy(ctx)
	if err != nil {
		cancel()
		m.Close()
		return nil, err
	}
	return func() { unlock(); cancel(); m.Close() }, nil
}
