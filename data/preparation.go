package data

import (
	"database/sql"
	"fmt"
	"time"

	"github.com/sirupsen/logrus"
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

	// ListPreparationRecords
	ListPreparationRecords() ([]*PreparationRecord, error)

	// UpdatePreparationRecordByUid
	UpdatePreparationRecordByUid(uid, status, errMsg string) error

	// UpdatePreparationPortByUid
	UpdatePreparationPortByUid(uid, port string) error

	// UpdatePreparationPidByUid
	UpdatePreparationPidByUid(uid, pid string) error
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
	if err != nil {
		logrus.Fatalln(err.Error())
	}
	// return directly if equal the current UserVersion
	if version == UserVersion {
		return
	}
	// check the table exists or not
	exists, err := s.PreparationTableExists()
	if err != nil {
		logrus.Fatalln(err.Error())
	}
	if exists {
		// execute alter sql if exists
		err := s.AlterPreparationTable(addPidColumn)
		if err != nil {
			logrus.Fatalln(err.Error())
		}
	} else {
		// execute create table
		err = s.InitPreparationTable()
		if err != nil {
			logrus.Fatalln(err.Error())
		}
	}
	// update userVersion to new
	err = s.UpdateUserVersion(UserVersion)
	if err != nil {
		logrus.Fatalln(err.Error())
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
	for rows.Next() {
		rows.Scan(&c)
		break
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
	if processId != "" || processName != "" {
		query = fmt.Sprintf(`%s and (pid = ? OR process = ?)`, query)
	}
	stmt, err := s.DB.Prepare(query)
	if err != nil {
		return nil, err
	}
	defer stmt.Close()
	var rows *sql.Rows
	if processId != "" || processName != "" {
		rows, err = stmt.Query(programType, processId, processName)
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

func (s *Source) ListPreparationRecords() ([]*PreparationRecord, error) {
	stmt, err := s.DB.Prepare(`SELECT * FROM preparation`)
	if err != nil {
		return nil, err
	}
	defer stmt.Close()
	rows, err := stmt.Query()
	defer rows.Close()
	return getPreparationRecordFrom(rows)
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
