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

package jvm

import (
	"context"
	"os"
	"strings"
	"testing"

	"github.com/chaosblade-io/chaosblade-spec-go/channel"
	"github.com/chaosblade-io/chaosblade-spec-go/spec"
	"github.com/prometheus/common/log"
)

func Test_getJavaBinAndJavaHome(t *testing.T) {
	javaProcessCommand := "/opt/java/bin/java"
	cl = &channel.MockLocalChannel{
		RunFunc: func(ctx context.Context, script, args string) *spec.Response {
			if strings.Contains(args, "-1") {
				return spec.ReturnFail(spec.Code[spec.GetProcessError], "process not found")
			}
			return spec.ReturnSuccess(javaProcessCommand)
		},
		GetPsArgsFunc: func() string {
			return ""
		},
	}
	err := os.Unsetenv("JAVA_HOME")
	if err != nil {
		t.Logf("remove JAVA_HOME env failed, %v", err)
	}
	type args struct {
		javaHome string
		ctx      context.Context
		pid      string
	}
	tests := []struct {
		name             string
		args             args
		expectedJavaBin  string
		expectedJavaHome string
	}{
		{
			name:             "javaHome flag value is empty, JAVA_HOME doesn't exist, java process exists",
			args:             args{"", context.TODO(), "1"},
			expectedJavaBin:  "/opt/java/bin/java",
			expectedJavaHome: "/opt/java",
		},
		{
			name:             "javaHome flag value is empty, JAVA_HOME doesn't exist, java process doesn't exist",
			args:             args{"", context.TODO(), "-1"},
			expectedJavaBin:  "java",
			expectedJavaHome: "",
		},
		{
			name:             "javaHome flag exists, JAVA_HOME doesn't exist, java process exists",
			args:             args{"/home/admin/java", context.TODO(), "1"},
			expectedJavaBin:  "/home/admin/java/bin/java",
			expectedJavaHome: "/home/admin/java",
		},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			javaBin, javaHome := getJavaBinAndJavaHome(tt.args.javaHome, tt.args.ctx, tt.args.pid)
			if javaBin != tt.expectedJavaBin {
				t.Errorf("getJavaBinAndJavaHome() javaBin = %v, expectedJavaBin %v", javaBin, tt.expectedJavaBin)
			}
			if javaHome != tt.expectedJavaHome {
				t.Errorf("getJavaBinAndJavaHome() javaHome = %v, expectedJavaHome %v", javaHome, tt.expectedJavaHome)
			}
		})
	}

	// set JAVA_HOME
	err = os.Setenv("JAVA_HOME", "/opt/chaos/java")
	if err != nil {
		log.Errorf("set JAVA_HOME env failed, %v", err)
	}
	testsWithJavaHome := []struct {
		name             string
		args             args
		expectedJavaBin  string
		expectedJavaHome string
	}{
		{
			name:             "javaHome flag value is empty, JAVA_HOME exists, java process exists",
			args:             args{"", context.TODO(), "1"},
			expectedJavaBin:  "/opt/chaos/java/bin/java",
			expectedJavaHome: "/opt/chaos/java",
		},
		{
			name:             "javaHome flag exits, JAVA_HOME exists, java process exists",
			args:             args{"/home/admin/java", context.TODO(), "1"},
			expectedJavaBin:  "/home/admin/java/bin/java",
			expectedJavaHome: "/home/admin/java",
		},
	}
	for _, tt := range testsWithJavaHome {
		t.Run(tt.name, func(t *testing.T) {
			javaBin, javaHome := getJavaBinAndJavaHome(tt.args.javaHome, tt.args.ctx, tt.args.pid)
			if javaBin != tt.expectedJavaBin {
				t.Errorf("getJavaBinAndJavaHome() javaBin = %v, expectedJavaBin %v", javaBin, tt.expectedJavaBin)
			}
			if javaHome != tt.expectedJavaHome {
				t.Errorf("getJavaBinAndJavaHome() javaHome = %v, expectedJavaHome %v", javaHome, tt.expectedJavaHome)
			}
		})
	}
}
