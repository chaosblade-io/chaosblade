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

package data

import (
	"os"
	"path/filepath"
	"testing"

	"github.com/chaosblade-io/chaosblade-spec-go/util"
)

func TestGetDataFilePath(t *testing.T) {
	// 保存原始环境变量
	originalEnv := os.Getenv("CHAOSBLADE_DATAFILE_PATH")
	defer func() {
		// 恢复原始环境变量
		if originalEnv == "" {
			os.Unsetenv("CHAOSBLADE_DATAFILE_PATH")
		} else {
			os.Setenv("CHAOSBLADE_DATAFILE_PATH", originalEnv)
		}
	}()

	tests := []struct {
		name           string
		envValue       string
		setupFunc      func() error
		cleanupFunc    func() error
		expectedPrefix string
		expectedSuffix string
		expectError    bool
	}{
		{
			name:           "no environment variable",
			envValue:       "",
			expectedPrefix: util.GetProgramPath(),
			expectedSuffix: dataFile,
		},
		{
			name:     "existing directory",
			envValue: "/tmp/test_existing_dir",
			setupFunc: func() error {
				return os.MkdirAll("/tmp/test_existing_dir", 0o755)
			},
			cleanupFunc: func() error {
				return os.RemoveAll("/tmp/test_existing_dir")
			},
			expectedPrefix: "/tmp/test_existing_dir",
			expectedSuffix: dataFile,
		},
		{
			name:     "non-existing directory",
			envValue: "/tmp/test_nonexistent_dir",
			cleanupFunc: func() error {
				return os.RemoveAll("/tmp/test_nonexistent_dir")
			},
			expectedPrefix: "/tmp/test_nonexistent_dir",
			expectedSuffix: dataFile,
		},
		{
			name:     "existing file",
			envValue: "/tmp/test_existing_file.db",
			setupFunc: func() error {
				file, err := os.Create("/tmp/test_existing_file.db")
				if err != nil {
					return err
				}
				return file.Close()
			},
			cleanupFunc: func() error {
				return os.Remove("/tmp/test_existing_file.db")
			},
			expectedPrefix: "/tmp/test_existing_file.db",
			expectedSuffix: "",
		},
		{
			name:     "non-existing file",
			envValue: "/tmp/test_nonexistent_file.db",
			cleanupFunc: func() error {
				return os.Remove("/tmp/test_nonexistent_file.db")
			},
			expectedPrefix: "/tmp/test_nonexistent_file.db",
			expectedSuffix: "",
		},
		{
			name:     "non-existing file with non-existing parent directory",
			envValue: "/tmp/test_deep/nested/path/file.db",
			cleanupFunc: func() error {
				return os.RemoveAll("/tmp/test_deep")
			},
			expectedPrefix: "/tmp/test_deep/nested/path/file.db",
			expectedSuffix: "",
		},
		{
			name:     "directory with extension (treated as file)",
			envValue: "/tmp/test_dir_with_ext.dat",
			cleanupFunc: func() error {
				return os.Remove("/tmp/test_dir_with_ext.dat")
			},
			expectedPrefix: "/tmp/test_dir_with_ext.dat",
			expectedSuffix: "",
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			// 设置环境变量
			if tt.envValue == "" {
				os.Unsetenv("CHAOSBLADE_DATAFILE_PATH")
			} else {
				os.Setenv("CHAOSBLADE_DATAFILE_PATH", tt.envValue)
			}

			// 执行设置函数
			if tt.setupFunc != nil {
				if err := tt.setupFunc(); err != nil {
					t.Fatalf("Setup failed: %v", err)
				}
			}

			// 执行清理函数
			if tt.cleanupFunc != nil {
				defer func() {
					if err := tt.cleanupFunc(); err != nil {
						t.Logf("Cleanup failed: %v", err)
					}
				}()
			}

			// 调用被测试的函数
			result := GetDataFilePath()

			// 验证结果
			if tt.expectedSuffix != "" {
				expected := filepath.Join(tt.expectedPrefix, tt.expectedSuffix)
				if result != expected {
					t.Errorf("GetDataFilePath() = %v, want %v", result, expected)
				}
			} else {
				if result != tt.expectedPrefix {
					t.Errorf("GetDataFilePath() = %v, want %v", result, tt.expectedPrefix)
				}
			}

			// 验证路径是否实际存在（对于目录和文件路径）
			if tt.expectedSuffix != "" {
				// 对于目录路径，检查目录是否存在
				if _, err := os.Stat(filepath.Dir(result)); err != nil {
					t.Errorf("Expected directory does not exist: %v", err)
				}
			} else if tt.envValue != "" {
				// 对于文件路径，检查父目录是否存在
				if _, err := os.Stat(filepath.Dir(result)); err != nil {
					t.Errorf("Expected parent directory does not exist: %v", err)
				}
			}
		})
	}
}

func TestGetDataFilePathWithPermissionError(t *testing.T) {
	// 保存原始环境变量
	originalEnv := os.Getenv("CHAOSBLADE_DATAFILE_PATH")
	defer func() {
		// 恢复原始环境变量
		if originalEnv == "" {
			os.Unsetenv("CHAOSBLADE_DATAFILE_PATH")
		} else {
			os.Setenv("CHAOSBLADE_DATAFILE_PATH", originalEnv)
		}
	}()

	// 测试权限不足的情况（在大多数系统上，/root 目录通常没有写权限）
	os.Setenv("CHAOSBLADE_DATAFILE_PATH", "/root/test_no_permission")

	// 调用函数，应该回退到默认路径
	result := GetDataFilePath()
	expected := filepath.Join(util.GetProgramPath(), dataFile)

	if result != expected {
		t.Errorf("GetDataFilePath() with permission error = %v, want %v", result, expected)
	}
}

func TestGetDataFilePathWithInvalidPath(t *testing.T) {
	// 保存原始环境变量
	originalEnv := os.Getenv("CHAOSBLADE_DATAFILE_PATH")
	defer func() {
		// 恢复原始环境变量
		if originalEnv == "" {
			os.Unsetenv("CHAOSBLADE_DATAFILE_PATH")
		} else {
			os.Setenv("CHAOSBLADE_DATAFILE_PATH", originalEnv)
		}
	}()

	// 测试无效路径的情况（使用一个不存在的设备路径）
	os.Setenv("CHAOSBLADE_DATAFILE_PATH", "/dev/null/invalid")

	// 调用函数，应该回退到默认路径
	result := GetDataFilePath()
	expected := filepath.Join(util.GetProgramPath(), dataFile)

	if result != expected {
		t.Errorf("GetDataFilePath() with invalid path = %v, want %v", result, expected)
	}
}
