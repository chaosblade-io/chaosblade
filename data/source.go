package data

import (
	"database/sql"
	"github.com/chaosblade-io/chaosblade/util"
	"path"
	"github.com/sirupsen/logrus"
	"sync"
	_ "github.com/mattn/go-sqlite3"
	"fmt"
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
	database, err := sql.Open("sqlite3", path.Join(util.GetProgramPath(), dataFile))
	if err != nil {
		logrus.Fatalf("open data file err, %s", err.Error())
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
