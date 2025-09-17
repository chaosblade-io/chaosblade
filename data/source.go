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
	"path"
	"sync"
	"unicode"

	"github.com/chaosblade-io/chaosblade-spec-go/log"

	"github.com/chaosblade-io/chaosblade-spec-go/util"
	_ "github.com/glebarez/sqlite"
)

const dataFile = "chaosblade.dat"

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

const tableExistsDQL = `SELECT count(*) AS c
	FROM sqlite_master
	WHERE type = "table"
	AND name = ?
`

func (s *Source) init() {
	s.CheckAndInitExperimentTable()
	s.CheckAndInitPreTable()
}

func getConnection() *sql.DB {
	database, err := sql.Open("sqlite", path.Join(util.GetProgramPath(), dataFile))
	if err != nil {
		log.Fatalf(context.Background(), "open data file err, %s", err.Error())
		//log.Error(err, "open data file err")
		//os.Exit(1)
	}
	return database
}

func (s *Source) Close() {
	if s.DB != nil {
		s.DB.Close()
	}
}

// GetUserVersion returns the user_version value
func (s *Source) GetUserVersion() (int, error) {
	userVerRows, err := s.DB.Query("PRAGMA user_version")
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
	_, err := s.DB.Exec(fmt.Sprintf("PRAGMA user_version=%d", version))
	return err
}

func UpperFirst(str string) string {
	return string(unicode.ToUpper(rune(str[0]))) + str[1:]
}

// ColumnExists checks if a column exists in the specified table
func (s *Source) ColumnExists(tableName, columnName string) (bool, error) {
	query := `SELECT COUNT(*) FROM pragma_table_info(?) WHERE name = ?`
	stmt, err := s.DB.Prepare(query)
	if err != nil {
		return false, fmt.Errorf("prepare column exists query err, %s", err)
	}
	defer stmt.Close()

	rows, err := stmt.Query(tableName, columnName)
	if err != nil {
		return false, fmt.Errorf("query column exists err, %s", err)
	}
	defer rows.Close()

	var count int
	if rows.Next() {
		err = rows.Scan(&count)
		if err != nil {
			return false, fmt.Errorf("scan column count err, %s", err)
		}
	}

	return count > 0, nil
}
