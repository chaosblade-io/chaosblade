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

package data

import (
	"context"
	"database/sql"
	"fmt"
	"github.com/chaosblade-io/chaosblade-spec-go/log"
	"strings"
	"time"
)

type PreparationRecord struct {
	Uid         string
	ProgramType string
	Process     string
	Port        string
	Pid         string
	Status      string
	Error       string
	CreateTime  string
	UpdateTime  string
}

type PreparationSource interface {
	// CheckAndInitPreTable
	CheckAndInitPreTable()

	// InitPreparationTable when first executed
	InitPreparationTable() error

	// PreparationTableExists return true if preparation exists, otherwise return false or error if execute sql exception
	PreparationTableExists() (bool, error)

	// InsertPreparationRecord
	InsertPreparationRecord(record *PreparationRecord) error

	// QueryPreparationByUid
	QueryPreparationByUid(uid string) (*PreparationRecord, error)

	// QueryRunningPreByTypeAndProcess
	QueryRunningPreByTypeAndProcess(programType string, processName, processId string) (*PreparationRecord, error)

	// UpdatePreparationRecordByUid
	UpdatePreparationRecordByUid(uid, status, errMsg string) error

	// UpdatePreparationPortByUid
	UpdatePreparationPortByUid(uid, port string) error

	// UpdatePreparationPidByUid
	UpdatePreparationPidByUid(uid, pid string) error

	// QueryPreparationRecords
	QueryPreparationRecords(target, status, action, flag, limit string, asc bool) ([]*PreparationRecord, error)
}

// UserVersion PRAGMA [database.]user_version
const UserVersion = 1

// addPidColumn sql
const addPidColumn = `ALTER TABLE preparation ADD COLUMN pid VARCHAR DEFAULT ""`

// preparationTableDDL
const preparationTableDDL = `CREATE TABLE IF NOT EXISTS preparation (
	id INTEGER PRIMARY KEY AUTOINCREMENT,
	uid VARCHAR(32) UNIQUE,
	program_type       VARCHAR NOT NULL,
	process    VARCHAR,
	port       VARCHAR,
	status     VARCHAR,
    error 	   VARCHAR,
	create_time VARCHAR,
	update_time VARCHAR,
	pid 	   VARCHAR
)`

var preIndexDDL = []string{
	`CREATE INDEX pre_uid_uidx ON preparation (uid)`,
	`CREATE INDEX pre_status_idx ON preparation (uid)`,
	`CREATE INDEX pre_type_process_idx ON preparation (program_type, process)`,
}

var insertPreDML = `INSERT INTO
	preparation (uid, program_type, process, port, status, error, create_time, update_time, pid)
	VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
`

func (s *Source) CheckAndInitPreTable() {
	// check user_version
	version, err := s.GetUserVersion()
	ctx := context.Background()
	if err != nil {
		log.Fatalf(ctx, err.Error())
		//log.Error(err, "GetUserVersion err")
		//os.Exit(1)
	}
	// return directly if equal the current UserVersion
	if version == UserVersion {
		return
	}
	// check the table exists or not
	exists, err := s.PreparationTableExists()
	if err != nil {
		log.Fatalf(ctx, err.Error())
		//log.Error(err, "PreparationTableExists err")
		//os.Exit(1)
	}
	if exists {
		// execute alter sql if exists
		err := s.AlterPreparationTable(addPidColumn)
		if err != nil {
			log.Fatalf(ctx, err.Error())
			//log.Error(err, "AlterPreparationTable err", "addPidColumn", addPidColumn)
			//os.Exit(1)
		}
	} else {
		// execute create table
		err = s.InitPreparationTable()
		if err != nil {
			log.Fatalf(ctx, err.Error())
			//log.Error(err, "InitPreparationTable err")
			//os.Exit(1)
		}
	}
	// update userVersion to new
	err = s.UpdateUserVersion(UserVersion)
	if err != nil {
		log.Fatalf(ctx, err.Error())
		//log.Error(err, "UpdateUserVersion err", "UserVersion", UserVersion)
		//os.Exit(1)
	}
}

func (s *Source) InitPreparationTable() error {
	_, err := s.DB.Exec(preparationTableDDL)
	if err != nil {
		return fmt.Errorf("create preparation table err, %s", err)
	}
	for _, sql := range preIndexDDL {
		s.DB.Exec(sql)
	}
	return nil
}

func (s *Source) AlterPreparationTable(alterSql string) error {
	_, err := s.DB.Exec(alterSql)
	if err != nil {
		return fmt.Errorf("execute %s sql err, %s", alterSql, err)
	}
	return nil
}

func (s *Source) PreparationTableExists() (bool, error) {
	stmt, err := s.DB.Prepare(tableExistsDQL)
	if err != nil {
		return false, fmt.Errorf("select preparation table exists err when invoke db prepare, %s", err)
	}
	defer stmt.Close()
	rows, err := stmt.Query("preparation")
	if err != nil {
		return false, fmt.Errorf("select preparation table exists or not err, %s", err)
	}
	defer rows.Close()
	var c int
	if rows.Next() {
		rows.Scan(&c)
	}

	return c != 0, nil
}

func (s *Source) InsertPreparationRecord(record *PreparationRecord) error {
	stmt, err := s.DB.Prepare(insertPreDML)
	if err != nil {
		return err
	}
	defer stmt.Close()
	_, err = stmt.Exec(
		record.Uid,
		record.ProgramType,
		record.Process,
		record.Port,
		record.Status,
		record.Error,
		record.CreateTime,
		record.UpdateTime,
		record.Pid,
	)
	if err != nil {
		return err
	}
	return nil
}

func (s *Source) QueryPreparationByUid(uid string) (*PreparationRecord, error) {
	stmt, err := s.DB.Prepare(`SELECT * FROM preparation WHERE uid = ?`)
	if err != nil {
		return nil, err
	}
	defer stmt.Close()
	rows, err := stmt.Query(uid)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	records, err := getPreparationRecordFrom(rows)
	if err != nil {
		return nil, err
	}
	if len(records) == 0 {
		return nil, nil
	}
	return records[0], nil
}

// QueryRunningPreByTypeAndProcess returns the first record matching the process id or process name
func (s *Source) QueryRunningPreByTypeAndProcess(programType string, processName, processId string) (*PreparationRecord, error) {
	var query = `SELECT * FROM preparation WHERE program_type = ? and status = "Running"`
	if processId != "" && processName != "" {
		query = fmt.Sprintf(`%s and pid = ? and process = ?`, query)
	} else if processId != "" {
		query = fmt.Sprintf(`%s and pid = ?`, query)
	} else if processName != "" {
		query = fmt.Sprintf(`%s and process = ?`, query)
	}
	stmt, err := s.DB.Prepare(query)
	if err != nil {
		return nil, err
	}
	defer stmt.Close()
	var rows *sql.Rows
	if processId != "" && processName != "" {
		rows, err = stmt.Query(programType, processId, processName)
	} else if processId != "" {
		rows, err = stmt.Query(programType, processId)
	} else if processName != "" {
		rows, err = stmt.Query(programType, processName)
	} else {
		rows, err = stmt.Query(programType)
	}
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	records, err := getPreparationRecordFrom(rows)
	if err != nil {
		return nil, err
	}
	if len(records) == 0 {
		return nil, nil
	}
	return records[0], nil
}

func getPreparationRecordFrom(rows *sql.Rows) ([]*PreparationRecord, error) {
	records := make([]*PreparationRecord, 0)
	for rows.Next() {
		var id int
		var uid, t, p, port, status, error, createTime, updateTime, pid string
		err := rows.Scan(&id, &uid, &t, &p, &port, &status, &error, &createTime, &updateTime, &pid)
		if err != nil {
			return nil, err
		}
		record := &PreparationRecord{
			Uid:         uid,
			ProgramType: t,
			Process:     p,
			Port:        port,
			Pid:         pid,
			Status:      status,
			Error:       error,
			CreateTime:  createTime,
			UpdateTime:  updateTime,
		}
		records = append(records, record)
	}
	return records, nil
}

func (s *Source) UpdatePreparationRecordByUid(uid, status, errMsg string) error {
	stmt, err := s.DB.Prepare(`UPDATE preparation
	SET status = ?, error = ?, update_time = ?
	WHERE uid = ?
`)
	if err != nil {
		return err
	}
	defer stmt.Close()
	_, err = stmt.Exec(status, errMsg, time.Now().Format(time.RFC3339Nano), uid)
	if err != nil {
		return err
	}
	return nil
}

func (s *Source) UpdatePreparationPortByUid(uid, port string) error {
	stmt, err := s.DB.Prepare(`UPDATE preparation
	SET port = ?, update_time = ?
	WHERE uid = ?
`)
	if err != nil {
		return err
	}
	defer stmt.Close()
	_, err = stmt.Exec(port, time.Now().Format(time.RFC3339Nano), uid)
	if err != nil {
		return err
	}
	return nil
}

func (s *Source) UpdatePreparationPidByUid(uid, pid string) error {
	stmt, err := s.DB.Prepare(`UPDATE preparation
	SET pid = ?, update_time = ?
	WHERE uid = ?
`)
	if err != nil {
		return err
	}
	defer stmt.Close()
	_, err = stmt.Exec(pid, time.Now().Format(time.RFC3339Nano), uid)
	if err != nil {
		return err
	}
	return nil
}

func (s *Source) QueryPreparationRecords(target, status, action, flag, limit string, asc bool) ([]*PreparationRecord, error) {
	sql := `SELECT * FROM preparation where 1=1`
	parameters := make([]interface{}, 0)
	if target != "" {
		sql = fmt.Sprintf(`%s and program_type = ?`, sql)
		parameters = append(parameters, target)
	}
	if status != "" {
		sql = fmt.Sprintf(`%s and status = ?`, sql)
		parameters = append(parameters, UpperFirst(status))
	}
	if action != "" {
		sql = fmt.Sprintf(`%s and sub_command = ?`, sql)
		parameters = append(parameters, action)
	}
	if flag != "" {
		sql = fmt.Sprintf(`%s and flag like ?`, sql)
		parameters = append(parameters, "%"+flag+"%")
	}
	if asc {
		sql = fmt.Sprintf(`%s order by id asc`, sql)
	} else {
		sql = fmt.Sprintf(`%s order by id desc`, sql)
	}
	if limit != "" {
		values := strings.Split(limit, ",")
		offset := "0"
		count := "0"
		if len(values) > 1 {
			offset = values[0]
			count = values[1]
		} else {
			count = values[0]
		}
		sql = fmt.Sprintf(`%s limit ?,?`, sql)
		parameters = append(parameters, offset, count)
	}
	stmt, err := s.DB.Prepare(sql)
	if err != nil {
		return nil, err
	}
	defer stmt.Close()
	rows, err := stmt.Query(parameters...)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	return getPreparationRecordFrom(rows)
}
