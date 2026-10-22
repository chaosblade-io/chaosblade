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
	"database/sql"
	"path/filepath"
	"testing"
)

// newTestPreparationSource creates a Source backed by a fresh temporary sqlite
// database that already contains the preparation table and one record.
func newTestPreparationSource(t *testing.T) *Source {
	t.Helper()
	dbPath := filepath.Join(t.TempDir(), "chaosblade.dat")
	db, err := sql.Open("sqlite", dbPath)
	if err != nil {
		t.Fatalf("open sqlite db err: %v", err)
	}
	t.Cleanup(func() { db.Close() })
	s := &Source{DB: db}
	if err := s.InitPreparationTable(); err != nil {
		t.Fatalf("init preparation table err: %v", err)
	}
	if err := s.InsertPreparationRecord(&PreparationRecord{
		Uid:         "uid-1",
		ProgramType: "jvm",
		Process:     "tomcat",
		Port:        "1234",
		Pid:         "100",
		Status:      "Running",
	}); err != nil {
		t.Fatalf("insert preparation record err: %v", err)
	}
	return s
}

// QueryPreparationRecords is reachable from `blade status --type prepare`,
// which forwards the --action and --flag-filter values to it. The preparation
// table has neither a sub_command nor a flag column, so filtering on those
// non-existent columns makes the query fail with a database error instead of
// returning the preparation records.
func TestQueryPreparationRecordsAction(t *testing.T) {
	s := newTestPreparationSource(t)
	records, err := s.QueryPreparationRecords("", "", "fullload", "", "", false)
	if err != nil {
		t.Fatalf("QueryPreparationRecords with action returned err: %v", err)
	}
	if len(records) != 1 {
		t.Fatalf("expected 1 preparation record, got %d", len(records))
	}
}

func TestQueryPreparationRecordsFlag(t *testing.T) {
	s := newTestPreparationSource(t)
	records, err := s.QueryPreparationRecords("", "", "", "debug", "", false)
	if err != nil {
		t.Fatalf("QueryPreparationRecords with flag returned err: %v", err)
	}
	if len(records) != 1 {
		t.Fatalf("expected 1 preparation record, got %d", len(records))
	}
}
