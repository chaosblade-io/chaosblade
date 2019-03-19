package data

import (
	"database/sql"
	"github.com/chaosblade-io/chaosblade/util"
	"path"
	"github.com/sirupsen/logrus"
	"sync"
	_ "github.com/mattn/go-sqlite3"
)

const dataFile = "chaosblade.dat"

type Source struct {
	DB *sql.DB
}

var source *Source
var once = sync.Once{}

func GetSource() *Source {
	once.Do(func() {
		source = &Source{
			DB: getConnection(),
		}
		source.init()
	})
	return source
}

const tableExistsDQL = `SELECT count(*) AS c
	FROM sqlite_master 
	WHERE type = "table"
	AND name = ?
`

func (s *Source) init() {
	s.checkAndInitExperimentTable()
	s.checkAndInitPreTable()
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
