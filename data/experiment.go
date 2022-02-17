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
	"database/sql"
	"fmt"
	"github.com/chaosblade-io/chaosblade-spec-go/spec"
	"github.com/sirupsen/logrus"
	"os"
	"strings"
	"time"
)

type ExperimentModel struct {
	Uid        string
	Command    string
	SubCommand string
	Flag       string
	Status     string
	Error      string
	CreateTime string
	UpdateTime string
}

type ExperimentSource interface {
	// CheckAndInitExperimentTable, if experiment table not exists, then init it
	CheckAndInitExperimentTable()

	// ExperimentTableExists return true if experiment exists
	ExperimentTableExists() (bool, error)

	// InitExperimentTable for first executed
	InitExperimentTable() error

	// InsertExperimentModel for creating chaos experiment
	InsertExperimentModel(model *ExperimentModel) error

	// UpdateExperimentModelByUid
	UpdateExperimentModelByUid(uid, status, errMsg string) error

	// QueryExperimentModelByUid
	QueryExperimentModelByUid(uid string) (*ExperimentModel, error)

	// QueryExperimentModels
	QueryExperimentModels(target, action, flag, status, limit string, asc bool) ([]*ExperimentModel, error)

	// QueryExperimentModelsByCommand
	// flags value contains necessary parameters generally
	QueryExperimentModelsByCommand(command, subCommand string, flags map[string]string) ([]*ExperimentModel, error)

	// DeleteExperimentModelByUid
	DeleteExperimentModelByUid(uid string) error
}

const expTableDDL = `CREATE TABLE IF NOT EXISTS experiment (
	id INTEGER PRIMARY KEY %s,
	uid VARCHAR(32) UNIQUE,
	command VARCHAR(16) NOT NULL,
	sub_command VARCHAR(16),
	flag VARCHAR(256),
	status VARCHAR(16),
	error VARCHAR(512),
	create_time VARCHAR(32),
	update_time VARCHAR(32)
)`

var expIndexDDL = []string{
	`CREATE INDEX exp_uid_uidx ON experiment (uid)`,
	`CREATE INDEX exp_command_idx ON experiment (command)`,
	`CREATE INDEX exp_status_idx ON experiment (status)`,
}

var insertExpDML = `INSERT INTO
	experiment (uid, command, sub_command, flag, status, error, create_time, update_time)
	VALUES (?, ?, ?, ?, ?, ?, ?, ?)
`

func (s *Source) CheckAndInitExperimentTable() {
	exists, err := s.ExperimentTableExists()
	if err != nil {
		logrus.Errorf("ExperimentTableExists err, %s", err.Error())
		fmt.Println(err.Error())
		os.Exit(-1)
	}
	if !exists {
		err = s.InitExperimentTable()
		if err != nil {
			logrus.Errorf("InitExperimentTable err, %s", err.Error())
			fmt.Println(err.Error())
			os.Exit(-1)
		}
	}
}

func (s *Source) ExperimentTableExists() (bool, error) {
	rows, err := s.queryTableSchema("experiment")
	if err != nil {
		stmt, err := s.getStmtPreparation()
		if err != nil {
			return false, err
		}
		defer stmt.Close()
		rows, err = stmt.Query("experiment")
		if err != nil {
			return false, fmt.Errorf("select experiment table exists err, %s", err)
		}
	}
	defer rows.Close()
	var c int
	for rows.Next() {
		rows.Scan(&c)
		break
	}
	return c != 0, nil
}

func (s *Source) InitExperimentTable() error {
	var err error
	// auto increment keywords in mysql is different from in sqlite3
	switch Type {
	case "mysql":
		_, err = s.DB.Exec(fmt.Sprintf(expTableDDL, "AUTO_INCREMENT"))
		break
	default:
		_, err = s.DB.Exec(fmt.Sprintf(expTableDDL, "AUTOINCREMENT"))
		break
	}
	if err != nil {
		return fmt.Errorf("create experiment table err, %s", err)
	}
	for _, sql := range expIndexDDL {
		s.DB.Exec(sql)
	}
	return nil
}

func (s *Source) InsertExperimentModel(model *ExperimentModel) error {
	_, err := s.DB.Exec(insertExpDML,
		model.Uid,
		model.Command,
		model.SubCommand,
		model.Flag,
		model.Status,
		model.Error,
		model.CreateTime,
		model.UpdateTime,
	)
	if err != nil {
		stmt, err := s.DB.Prepare(insertExpDML)
		if err != nil {
			return err
		}
		defer stmt.Close()
		_, err = stmt.Exec(
			model.Uid,
			model.Command,
			model.SubCommand,
			model.Flag,
			model.Status,
			model.Error,
			model.CreateTime,
			model.UpdateTime,
		)
		if err != nil {
			return err
		}
	}
	return nil
}

func (s *Source) UpdateExperimentModelByUid(uid, status, errMsg string) error {
	_, err := s.DB.Exec(`UPDATE experiment SET status = ?, error = ?, update_time = ? WHERE uid = ?`,
		status, errMsg, time.Now().Format(time.RFC3339Nano), uid)
	if err != nil {
		stmt, err := s.DB.Prepare(`UPDATE experiment SET status = ?, error = ?, update_time = ? WHERE uid = ?`)
		if err != nil {
			return err
		}
		defer stmt.Close()
		_, err = stmt.Exec(status, errMsg, time.Now().Format(time.RFC3339Nano), uid)
		if err != nil {
			return err
		}
	}
	return nil
}

func (s *Source) QueryExperimentModelByUid(uid string) (*ExperimentModel, error) {
	rows, err := s.DB.Query(`SELECT * FROM experiment WHERE uid = ?`, uid)
	if err != nil {
		stmt, err := s.DB.Prepare(`SELECT * FROM experiment WHERE uid = ?`)
		if err != nil {
			return nil, err
		}
		defer stmt.Close()
		rows, err = stmt.Query(uid)
		if err != nil {
			return nil, err
		}
	}
	defer rows.Close()
	models, err := getExperimentModelsFrom(rows)
	if err != nil {
		return nil, err
	}
	if len(models) == 0 {
		return nil, nil
	}
	return models[0], nil
}

func (s *Source) QueryExperimentModels(target, action, flag, status, limit string, asc bool) ([]*ExperimentModel, error) {
	sql := `SELECT * FROM experiment where 1=1`
	parameters := make([]interface{}, 0)
	if target != "" {
		sql = fmt.Sprintf(`%s and command = ?`, sql)
		parameters = append(parameters, target)
	}
	if action != "" {
		sql = fmt.Sprintf(`%s and sub_command = ?`, sql)
		parameters = append(parameters, action)
	}
	if flag != "" {
		sql = fmt.Sprintf(`%s and flag like ?`, sql)
		parameters = append(parameters, "%"+flag+"%")
	}
	if status != "" {
		sql = fmt.Sprintf(`%s and status = ?`, sql)
		parameters = append(parameters, UpperFirst(status))
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
	rows, err := s.DB.Query(sql, parameters...)
	if err != nil {
		stmt, err := s.DB.Prepare(sql)
		if err != nil {
			return nil, err
		}
		defer stmt.Close()
		rows, err = stmt.Query(parameters...)
		if err != nil {
			return nil, err
		}
	}
	defer rows.Close()
	return getExperimentModelsFrom(rows)
}

func (s *Source) QueryExperimentModelsByCommand(command, subCommand string, flags map[string]string) ([]*ExperimentModel, error) {
	models := make([]*ExperimentModel, 0)
	experimentModels, err := s.QueryExperimentModels(command, subCommand, "", "", "", true)
	if err != nil {
		return models, err
	}
	if flags == nil || len(flags) == 0 {
		return experimentModels, nil
	}
	for _, experimentModel := range experimentModels {
		recordModel := spec.ConvertCommandsToExpModel(subCommand, command, experimentModel.Flag)
		recordFlags := recordModel.ActionFlags
		isMatched := true
		for k, v := range flags {
			if v == "" {
				continue
			}
			if recordFlags[k] != v {
				isMatched = false
				break
			}
		}
		if isMatched {
			models = append(models, experimentModel)
		}
	}
	return models, nil
}

func getExperimentModelsFrom(rows *sql.Rows) ([]*ExperimentModel, error) {
	models := make([]*ExperimentModel, 0)
	for rows.Next() {
		var id int
		var uid, command, subCommand, flag, status, error, createTime, updateTime string
		err := rows.Scan(&id, &uid, &command, &subCommand, &flag, &status, &error, &createTime, &updateTime)
		if err != nil {
			return nil, err
		}
		model := &ExperimentModel{
			Uid:        uid,
			Command:    command,
			SubCommand: subCommand,
			Flag:       flag,
			Status:     status,
			Error:      error,
			CreateTime: createTime,
			UpdateTime: updateTime,
		}
		models = append(models, model)
	}
	return models, nil
}

func (s *Source) DeleteExperimentModelByUid(uid string) error {
	_, err := s.DB.Exec(`DELETE FROM experiment WHERE uid = ?`, uid)
	if err != nil {
		stmt, err := s.DB.Prepare(`DELETE FROM experiment WHERE uid = ?`)
		if err != nil {
			return err
		}
		defer stmt.Close()
		_, err = stmt.Exec(uid)
		if err != nil {
			return err
		}
	}
	return nil
}
