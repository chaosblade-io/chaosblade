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
	"fmt"
	"os"
	"testing"
)

func Test_getJavaBinAndJavaHome(t *testing.T) {
	err := os.Unsetenv("JAVA_HOME")
	if err != nil {
		t.Logf("remove JAVA_HOME env failed, %v", err)
	}
	type args struct {
		javaHome       string
		ctx            context.Context
		getJavaCmdLine func(pid string) (commandSlice []string, err error)
	}
	tests := []struct {
		name             string
		args             args
		expectedJavaBin  string
		expectedJavaHome string
	}{
		{
			name: "javaHome flag value is empty, JAVA_HOME doesn't exist, java process exists",
			args: args{"", context.TODO(), func(pid string) (commandSlice []string, err error) {
				return []string{"/opt/java/bin/java", "-jar", "xxx.jar"}, nil
			}},
			expectedJavaBin:  "/opt/java/bin/java",
			expectedJavaHome: "/opt/java",
		},
		{
			name: "javaHome flag value is empty, JAVA_HOME doesn't exist, java process doesn't exist",
			args: args{"", context.TODO(), func(pid string) (commandSlice []string, err error) {
				return nil, fmt.Errorf("process not found")
			}},
			expectedJavaBin:  "java",
			expectedJavaHome: "",
		},
		{
			name: "javaHome flag exists, JAVA_HOME doesn't exist, java process exists",
			args: args{"/home/admin/java", context.TODO(), func(pid string) (commandSlice []string, err error) {
				return []string{"/opt/java/bin/java", "-jar", "xxx.jar"}, nil
			}},
			expectedJavaBin:  "/home/admin/java/bin/java",
			expectedJavaHome: "/home/admin/java",
		},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			javaBin, javaHome := getJavaBinAndJavaHome(tt.args.javaHome, "", tt.args.getJavaCmdLine)
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
		t.Errorf("set JAVA_HOME env failed, %v", err)
	}
	testsWithJavaHome := []struct {
		name             string
		args             args
		expectedJavaBin  string
		expectedJavaHome string
	}{
		{
			name: "javaHome flag value is empty, JAVA_HOME exists, java process exists",
			args: args{"", context.TODO(), func(pid string) (commandSlice []string, err error) {
				return []string{"/opt/java/bin/java", "-jar", "xxx.jar"}, nil
			}},
			expectedJavaBin:  "/opt/chaos/java/bin/java",
			expectedJavaHome: "/opt/chaos/java",
		},
		{
			name: "javaHome flag exits, JAVA_HOME exists, java process exists",
			args: args{"/home/admin/java", context.TODO(), func(pid string) (commandSlice []string, err error) {
				return []string{"/opt/java/bin/java", "-jar", "xxx.jar"}, nil
			}},
			expectedJavaBin:  "/home/admin/java/bin/java",
			expectedJavaHome: "/home/admin/java",
		},
	}
	for _, tt := range testsWithJavaHome {
		t.Run(tt.name, func(t *testing.T) {
			javaBin, javaHome := getJavaBinAndJavaHome(tt.args.javaHome, "", tt.args.getJavaCmdLine)
			if javaBin != tt.expectedJavaBin {
				t.Errorf("getJavaBinAndJavaHome() javaBin = %v, expectedJavaBin %v", javaBin, tt.expectedJavaBin)
			}
			if javaHome != tt.expectedJavaHome {
				t.Errorf("getJavaBinAndJavaHome() javaHome = %v, expectedJavaHome %v", javaHome, tt.expectedJavaHome)
			}
		})
	}
}
