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
	"os"
	"path"
	"sync"
	"unicode"

	_ "github.com/go-sql-driver/mysql"
	_ "github.com/mattn/go-sqlite3"
	"github.com/sirupsen/logrus"
)

const dataFile = "chaosblade.dat"
var (
	Type string
	Host string
	Port int
	Database string
	Username string
	Password string
	Timeout int
	DatPath string
)

type SourceI interface {
	ExperimentSource
	PreparationSource
}

type Source struct {
	DB *sql.DB
}

var source SourceI
var once = sync.Once{}

func GetSource() SourceI {
	once.Do(func() {
		src := &Source{
			DB: getConnection(),
		}
		src.init()
		source = src
	})
	return source
}

func (s *Source) getStmtPreparation() (*sql.Stmt, error) {
	var stmt *sql.Stmt
	var err error
	switch Type {
	case "mysql":
		stmt, err = s.DB.Prepare(fmt.Sprintf(
			`SELECT count(*) AS c
				FROM information_schema.tables
				WHERE TABLE_SCHEMA = "%s" AND TABLE_NAME = ?`, Database),
		)
		break
	default:
		stmt, err = s.DB.Prepare(fmt.Sprintf(
			`SELECT count(*) AS c
				FROM sqlite_master
				WHERE type = "table" AND name = ?`),
		)
		break
	}
	if err != nil {
		return nil, fmt.Errorf("select experiment table exists err when invoke db prepare, %s", err)
	}
	return stmt, nil
}

func (s *Source) init() {
	s.CheckAndInitExperimentTable()
	s.CheckAndInitPreTable()
}

func getConnection() *sql.DB {
	var database *sql.DB
	var err error
	switch Type {
	case "mysql":
		database, err = sql.Open("mysql",
			fmt.Sprintf("%v:%s@tcp(%s:%d)/%v?charset=utf8&parseTime=true&interpolateParams=true&timeout=%ds&readTimeout=%ds&writeTimeout=%ds",
			Username, Password, Host, Port, Database, Timeout, Timeout, Timeout))
		break
	default:
		if _, err := os.Stat(DatPath); err != nil {
			logrus.Errorf("stat dat-path failed: %s", err.Error())
			fmt.Println(err.Error())
			os.Exit(-1)
		}
		database, err = sql.Open("sqlite3", path.Join(DatPath, dataFile))
		break
	}
	if err != nil {
		logrus.Errorf("open database err, %s", err.Error())
		fmt.Println(err.Error())
		os.Exit(-1)
	}
	database.SetMaxOpenConns(20)
	database.SetMaxIdleConns(2)
	return database
}

func (s *Source) Close() {
	if s.DB != nil {
		s.DB.Close()
	}
}

// GetUserVersion returns the user_version value
func (s *Source) GetUserVersion() (int, error) {
	var userVerRows *sql.Rows
	var err error
	switch Type {
	case "mysql":
		userVerRows, err = s.DB.Query("SELECT @user_version")
		break
	default:
		userVerRows, err = s.DB.Query("PRAGMA user_version")
		break
	}
	if err != nil {
		return 0, err
	}
	defer userVerRows.Close()
	var userVersion int
	for userVerRows.Next() {
		userVerRows.Scan(&userVersion)
	}
	return userVersion, nil
}

// UpdateUserVersion to the latest
func (s *Source) UpdateUserVersion(version int) error {
	var err error
	switch Type {
	case "mysql":
		_, err = s.DB.Exec(fmt.Sprintf("SET @user_version=%d", version))
		break
	default:
		_, err = s.DB.Exec(fmt.Sprintf("PRAGMA user_version=%d", version))
		break
	}
	return err
}

func UpperFirst(str string) string {
	return string(unicode.ToUpper(rune(str[0]))) + str[1:]
}
