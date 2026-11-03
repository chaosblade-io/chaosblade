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
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/tls"
	"crypto/x509"
	"crypto/x509/pkix"
	"encoding/pem"
	"io"
	"log"
	"math/big"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"testing"
	"time"
)

func testTLS(t *testing.T) (*tls.Config, *tls.Config) {
	t.Helper()
	dir := t.TempDir()
	caKey, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	ca := &x509.Certificate{SerialNumber: big.NewInt(1), Subject: pkix.Name{CommonName: "recovery test CA"}, NotBefore: time.Now().Add(-time.Hour), NotAfter: time.Now().Add(time.Hour), IsCA: true, BasicConstraintsValid: true, KeyUsage: x509.KeyUsageCertSign}
	der, err := x509.CreateCertificate(rand.Reader, ca, ca, &caKey.PublicKey, caKey)
	if err != nil {
		t.Fatal(err)
	}
	caFile := filepath.Join(dir, "ca.pem")
	if err = os.WriteFile(caFile, pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: der}), 0o600); err != nil {
		t.Fatal(err)
	}
	makeConfig := func(server bool) *tls.Config {
		key, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
		if err != nil {
			t.Fatal(err)
		}
		eku := x509.ExtKeyUsageClientAuth
		name := "client"
		if server {
			eku = x509.ExtKeyUsageServerAuth
			name = "server"
		}
		leaf := &x509.Certificate{SerialNumber: big.NewInt(2), Subject: pkix.Name{CommonName: name}, DNSNames: []string{"recovery.test"}, NotBefore: ca.NotBefore, NotAfter: ca.NotAfter, KeyUsage: x509.KeyUsageDigitalSignature, ExtKeyUsage: []x509.ExtKeyUsage{eku}}
		cert, err := x509.CreateCertificate(rand.Reader, leaf, ca, &key.PublicKey, caKey)
		if err != nil {
			t.Fatal(err)
		}
		private, err := x509.MarshalECPrivateKey(key)
		if err != nil {
			t.Fatal(err)
		}
		files := TLSFiles{CA: caFile, Certificate: filepath.Join(dir, name+".crt"), Key: filepath.Join(dir, name+".key")}
		for path, data := range map[string][]byte{files.Certificate: pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: cert}), files.Key: pem.EncodeToMemory(&pem.Block{Type: "EC PRIVATE KEY", Bytes: private})} {
			if err = os.WriteFile(path, data, 0o600); err != nil {
				t.Fatal(err)
			}
		}
		config, err := TLSConfig(files, server)
		if err != nil {
			t.Fatal(err)
		}
		return config
	}
	return makeConfig(true), makeConfig(false)
}

func TestMTLSStopAndIsolation(t *testing.T) {
	serverTLS, clientTLS := testTLS(t)
	f := &fakeBackend{}
	m := openTest(t, t.TempDir(), f)
	server := httptest.NewUnstartedServer(m.Handler())
	server.TLS = serverTLS
	server.Config.ErrorLog = log.New(io.Discard, "", 0)
	server.StartTLS()
	defer server.Close()
	p := testPlan()
	p.Nodes[0].Address = server.URL
	registerTest(t, m, p)
	if _, err := m.Apply(context.Background(), p.RunID, "target"); err != nil {
		t.Fatal(err)
	}
	c := &Client{TLS: clientTLS}
	discovered, err := c.Discover(context.Background(), Profile{ClusterID: p.ClusterID, Nodes: p.Nodes}, p.Name, "")
	if err != nil || Digest(discovered) != Digest(p) {
		t.Fatalf("offline discovery: %+v %v", discovered, err)
	}
	wrong := testPlan()
	wrong.Nodes = p.Nodes
	wrong.ClusterID = "other-cluster"
	if _, err := c.Run(context.Background(), wrong, true); err == nil {
		t.Fatal("cross-cluster stop accepted")
	}
	if f.recovers != 0 {
		t.Fatal("unauthorized request changed the fault")
	}
	noCert := clientTLS.Clone()
	noCert.Certificates = nil
	if _, err := (&Client{TLS: noCert}).Run(context.Background(), p, true); err == nil {
		t.Fatal("client without certificate accepted")
	}
	badName := p
	badName.Nodes = append([]Node(nil), p.Nodes...)
	badName.Nodes[0].ServerName = "other.test"
	if _, err := c.Run(context.Background(), badName, true); err == nil {
		t.Fatal("wrong TLS server identity accepted")
	}
	result, err := c.Run(context.Background(), p, true)
	if err != nil || !result.Complete {
		t.Fatalf("stop failed: %+v %v", result, err)
	}
	if f.recovers != 1 {
		t.Fatal("fault not recovered exactly once")
	}
	observed, err := c.Run(context.Background(), p, false)
	if err != nil || !observed.Complete {
		t.Fatalf("status did not report completed recovery: %+v %v", observed, err)
	}
	config := clientTLS.Clone()
	config.ServerName = "recovery.test"
	transport := &http.Transport{TLSClientConfig: config}
	defer transport.CloseIdleConnections()
	request, _ := http.NewRequest(http.MethodPost, server.URL+"/v1/runs/run/apply", nil)
	request.Header.Set("X-ChaosBlade-Cluster", "cluster")
	request.Header.Set("X-ChaosBlade-Node", "node")
	response, err := (&http.Client{Transport: transport}).Do(request)
	if err != nil {
		t.Fatal(err)
	}
	response.Body.Close()
	if response.StatusCode != http.StatusMethodNotAllowed {
		t.Fatal("unexpected mutation endpoint")
	}
}

func TestClientReportsPartialRecoveryAndStillCleansReachableNode(t *testing.T) {
	serverTLS, clientTLS := testTLS(t)
	f := &fakeBackend{}
	m := openTest(t, t.TempDir(), f)
	server := httptest.NewUnstartedServer(m.Handler())
	server.TLS = serverTLS
	server.StartTLS()
	defer server.Close()
	p := testPlan()
	p.Nodes[0].Address = server.URL
	p.Nodes = append(p.Nodes, Node{UID: "offline", Name: "offline", Address: "https://127.0.0.1:1", ServerName: "recovery.test"})
	p.Targets = append(p.Targets, Target{ID: "offline-target", NodeUID: "offline", Scope: "node", Action: "loss", Flags: map[string]string{"interface": "eth0", "percent": "10"}})
	registerTest(t, m, p)
	_, _ = m.Apply(context.Background(), p.RunID, "target")
	result, err := (&Client{TLS: clientTLS}).Run(context.Background(), p, true)
	if err == nil || result.Complete || len(result.Nodes) != 2 || !Complete(p, result.Nodes[0]) || result.Nodes[1].Error == "" {
		t.Fatalf("partial response: %+v %v", result, err)
	}
	if f.recovers != 1 {
		t.Fatal("reachable node was not cleaned")
	}
}
