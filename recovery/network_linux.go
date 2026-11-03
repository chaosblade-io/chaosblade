//go:build linux

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

package recovery

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"syscall"
	"time"

	"github.com/containerd/containerd"
	"github.com/containerd/containerd/namespaces"
	"github.com/docker/docker/client"

	"github.com/chaosblade-io/chaosblade-spec-go/spec"
)

// NetworkBackend reuses chaos_os for injection and its inverse. No remote input
// can select an executable, shell command, runtime socket or arbitrary PID.
type NetworkBackend struct{ Directory string }

func NewNetworkBackend(directory string) Backend { return &NetworkBackend{Directory: directory} }

func (b *NetworkBackend) Resolve(ctx context.Context, t Target) (Identity, error) {
	pid := os.Getpid()
	if t.Scope == "pod" {
		var err error
		pid, err = containerPID(ctx, t)
		if err != nil {
			return Identity{}, err
		}
	}
	boot, err := os.ReadFile("/proc/sys/kernel/random/boot_id")
	if err != nil {
		return Identity{}, err
	}
	f, err := os.Open(fmt.Sprintf("/proc/%d/ns/net", pid))
	if err != nil {
		return Identity{}, err
	}
	defer f.Close()
	st, err := f.Stat()
	if err != nil {
		return Identity{}, err
	}
	id := Identity{BootID: strings.TrimSpace(string(boot)), NetNS: strconv.FormatUint(st.Sys().(*syscall.Stat_t).Ino, 10), PID: pid, Interface: t.Flags["interface"]}
	// Node identity must survive the short-lived registration CLI process.
	if t.Scope == "node" {
		id.PID = 1
	}
	out, err := b.command(ctx, f, "ip", "-j", "link", "show", "dev", id.Interface)
	if err != nil {
		return Identity{}, err
	}
	var links []struct {
		Index int `json:"ifindex"`
		Queue int `json:"txqlen"`
	}
	if err = json.Unmarshal(out, &links); err != nil || len(links) != 1 {
		return Identity{}, fmt.Errorf("cannot identify network interface")
	}
	id.InterfaceIndex = links[0].Index
	id.TxQueueLength = links[0].Queue
	return id, nil
}

func containerPID(ctx context.Context, t Target) (int, error) {
	switch t.Runtime {
	case "containerd":
		c, err := containerd.New("/run/containerd/containerd.sock")
		if err != nil {
			return 0, err
		}
		defer c.Close()
		ctx = namespaces.WithNamespace(ctx, "k8s.io")
		container, err := c.LoadContainer(ctx, t.ContainerID)
		if err != nil {
			return 0, err
		}
		labels, err := container.Labels(ctx)
		if err != nil {
			return 0, err
		}
		// Kubernetes containerd stores pod identity in the OCI spec annotations.
		podUID := labels["io.kubernetes.pod.uid"]
		if podUID == "" {
			s, err := container.Spec(ctx)
			if err != nil {
				return 0, err
			}
			if s == nil {
				return 0, fmt.Errorf("container OCI specification is unavailable")
			}
			podUID = s.Annotations["io.kubernetes.cri.sandbox-uid"]
		}
		if podUID != t.PodUID {
			return 0, fmt.Errorf("container pod UID does not match plan")
		}
		task, err := container.Task(ctx, nil)
		if err != nil {
			return 0, err
		}
		if task.Pid() == 0 {
			return 0, fmt.Errorf("container is not running")
		}
		return int(task.Pid()), nil
	case "docker":
		c, err := client.NewClientWithOpts(client.WithHost("unix:///var/run/docker.sock"), client.WithAPIVersionNegotiation())
		if err != nil {
			return 0, err
		}
		defer c.Close()
		info, err := c.ContainerInspect(ctx, t.ContainerID)
		if err != nil {
			return 0, err
		}
		if info.ContainerJSONBase == nil || info.Config == nil || info.ID != t.ContainerID || info.Config.Labels["io.kubernetes.pod.uid"] != t.PodUID || info.State == nil || !info.State.Running {
			return 0, fmt.Errorf("container identity or running state does not match plan")
		}
		return info.State.Pid, nil
	default:
		return 0, fmt.Errorf("unsupported container runtime")
	}
}

func (b *NetworkBackend) namespace(ctx context.Context, r Record) (*os.File, error) {
	if r.Target.Scope == "pod" {
		pid, err := containerPID(ctx, r.Target)
		if err != nil {
			return nil, fmt.Errorf("original container identity unavailable: %w", err)
		}
		if pid != r.Identity.PID {
			return nil, fmt.Errorf("container process identity changed")
		}
	}
	boot, err := os.ReadFile("/proc/sys/kernel/random/boot_id")
	if err != nil {
		return nil, err
	}
	if strings.TrimSpace(string(boot)) != r.Identity.BootID {
		return nil, fmt.Errorf("node boot identity changed")
	}
	f, err := os.Open(fmt.Sprintf("/proc/%d/ns/net", r.Identity.PID))
	if err != nil {
		return nil, fmt.Errorf("original network namespace unavailable: %w", err)
	}
	st, err := f.Stat()
	if err != nil || strconv.FormatUint(st.Sys().(*syscall.Stat_t).Ino, 10) != r.Identity.NetNS {
		f.Close()
		return nil, fmt.Errorf("network namespace identity changed")
	}
	out, err := b.command(ctx, f, "ip", "-j", "link", "show", "dev", r.Identity.Interface)
	var links []struct {
		Index int `json:"ifindex"`
	}
	if err != nil || json.Unmarshal(out, &links) != nil || len(links) != 1 || links[0].Index != r.Identity.InterfaceIndex {
		f.Close()
		return nil, fmt.Errorf("network interface identity changed")
	}
	return f, nil
}

func (b *NetworkBackend) command(ctx context.Context, f *os.File, program string, args ...string) ([]byte, error) {
	cmd := exec.CommandContext(ctx, "nsenter", append([]string{"--net=/proc/self/fd/3", "--", program}, args...)...)
	configureNetworkCommand(cmd)
	cmd.ExtraFiles = []*os.File{f}
	out, err := cmd.CombinedOutput()
	if err != nil {
		return nil, fmt.Errorf("network command failed: %w: %.2048s", err, out)
	}
	return out, nil
}

func (b *NetworkBackend) Inspect(ctx context.Context, r Record) (string, bool, error) {
	f, err := b.namespace(ctx, r)
	if err != nil {
		return "", false, err
	}
	defer f.Close()
	tc := filepath.Join(b.Directory, "bin", "tc")
	if _, err := os.Stat(tc); err != nil {
		tc = "tc"
	}
	out, err := b.command(ctx, f, tc, "-j", "qdisc", "show", "dev", r.Identity.Interface)
	if err != nil {
		return "", false, err
	}
	fp, clean, err := qdiscFingerprint(out, r.Target.Action)
	if err != nil {
		return "", false, err
	}
	queue, err := b.queueLength(ctx, f, r.Identity.Interface)
	if err != nil {
		return "", false, err
	}
	if clean {
		if r.Fingerprint == "" && queue != r.Identity.TxQueueLength {
			return "", false, fmt.Errorf("unverified transmit queue change")
		}
		return "", true, nil
	}
	return fmt.Sprintf("%s/%d", fp, queue), false, nil
}

func (b *NetworkBackend) execute(ctx context.Context, r Record, mode string) error {
	f, err := b.namespace(ctx, r)
	if err != nil {
		return err
	}
	defer f.Close()
	args := []string{mode, "network", r.Target.Action, "--uid=" + r.Target.ID}
	keys := make([]string, 0, len(r.Target.Flags))
	for k := range r.Target.Flags {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	for _, k := range keys {
		args = append(args, "--"+k+"="+r.Target.Flags[k])
	}
	out, err := b.command(ctx, f, filepath.Join(b.Directory, "bin", "chaos_os"), args...)
	if err != nil {
		return err
	}
	resp := spec.Decode(string(out), nil)
	if !resp.Success {
		return fmt.Errorf("network executor: %s", resp.Err)
	}
	return nil
}

func (b *NetworkBackend) Apply(ctx context.Context, r Record) error {
	return b.execute(ctx, r, "create")
}

func (b *NetworkBackend) Recover(ctx context.Context, r Record) error {
	_, clean, err := b.Inspect(ctx, r)
	if err != nil {
		return err
	}
	if !clean {
		if err = b.execute(ctx, r, "destroy"); err != nil {
			return err
		}
	}
	f, err := b.namespace(ctx, r)
	if err != nil {
		return err
	}
	defer f.Close()
	current, err := b.queueLength(ctx, f, r.Identity.Interface)
	if err != nil {
		return err
	}
	if current == r.Identity.TxQueueLength {
		return nil
	}
	// The existing executor can change a zero transmit queue to 1000. Only undo
	// that known change; leave any other administrator/CNI modification untouched.
	if current != 1000 {
		return fmt.Errorf("transmit queue changed outside this execution")
	}
	_, err = b.command(ctx, f, "ip", "link", "set", "dev", r.Identity.Interface, "txqueuelen", strconv.Itoa(r.Identity.TxQueueLength))
	return err
}

func (b *NetworkBackend) queueLength(ctx context.Context, f *os.File, iface string) (int, error) {
	data, err := b.command(ctx, f, "ip", "-j", "link", "show", "dev", iface)
	if err != nil {
		return 0, err
	}
	var links []struct {
		Queue int `json:"txqlen"`
	}
	if err = json.Unmarshal(data, &links); err != nil || len(links) != 1 {
		return 0, fmt.Errorf("cannot inspect transmit queue")
	}
	return links[0].Queue, nil
}

// qdiscFingerprint deliberately accepts only a single root netem qdisc. Unknown
// schedulers and filters are outside v1, because the existing inverse deletes root.
func qdiscFingerprint(data []byte, action string) (string, bool, error) {
	var q []map[string]interface{}
	if err := json.Unmarshal(data, &q); err != nil {
		return "", false, err
	}
	if len(q) == 1 && q[0]["kind"] == "noqueue" && q[0]["handle"] == "0:" {
		return "", true, nil
	}
	if len(q) != 1 || q[0]["kind"] != "netem" || q[0]["root"] != true {
		return "", false, fmt.Errorf("unsupported or foreign qdisc topology")
	}
	options, ok := q[0]["options"].(map[string]interface{})
	if !ok {
		return "", false, fmt.Errorf("netem options cannot be verified")
	}
	key := "delay"
	if action == "loss" {
		key = "loss-random"
	}
	// iproute2 versions print loss as either loss-random or loss.
	value, exists := options[key]
	if !exists && action == "loss" {
		value, exists = options["loss"]
	}
	if !exists || value == nil {
		return "", false, fmt.Errorf("requested netem action is absent from readback")
	}
	for _, k := range []string{"refcnt", "bytes", "packets", "drops", "overlimits", "requeues", "backlog", "qlen"} {
		delete(q[0], k)
	}
	canonical, err := json.Marshal(q)
	if err != nil {
		return "", false, err
	}
	sum := sha256.Sum256(canonical)
	return hex.EncodeToString(sum[:]), false, nil
}

// Network executors are synchronous. Cancellation must terminate their shell/tc
// descendants as well as the immediate process before releasing the effect lock.
func configureNetworkCommand(cmd *exec.Cmd) {
	cmd.SysProcAttr = &syscall.SysProcAttr{Setpgid: true}
	cmd.Cancel = func() error {
		err := syscall.Kill(-cmd.Process.Pid, syscall.SIGKILL)
		if errors.Is(err, syscall.ESRCH) {
			return os.ErrProcessDone
		}
		return err
	}
	cmd.WaitDelay = 2 * time.Second
}
