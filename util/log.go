package util

import (
	"github.com/sirupsen/logrus"
	"time"
	"gopkg.in/natefinch/lumberjack.v2"
	"os"
	"path"
	"io"
	"flag"
)

const (
	Blade = 1
	Bin   = 2
)

var Debug bool

func AddDebugFlag() {
	flag.BoolVar(&Debug, "debug", false, "set debug mode")
}

// InitLog invoked after flag parsed
func InitLog(programType int) {
	logFile, err := GetLogFile(programType)
	if err != nil {
		return
	}
	output := &lumberjack.Logger{
		Filename:   logFile,
		MaxSize:    30, // m
		MaxBackups: 1,
		MaxAge:     2, // days
		Compress:   false,
	}
	logrus.SetOutput(&fileWriterWithoutErr{output})

	formatter := &logrus.TextFormatter{
		FullTimestamp:   true,
		TimestampFormat: time.RFC3339Nano,
	}
	logrus.SetFormatter(formatter)

	if Debug {
		logrus.SetLevel(logrus.DebugLevel)
	}
}

// GetLogFile
func GetLogFile(programType int) (string, error) {
	var binDir string
	switch programType {
	case Blade:
		binDir = GetProgramPath()
	case Bin:
		binDir = GetProgramParentPath()
	default:
		binDir = GetProgramPath()
	}
	logsPath := path.Join(binDir, "logs")
	logFile := path.Join(logsPath, "chaosblade.log")
	if IsExist(logFile) {
		return logFile, nil
	}
	// mk dir
	err := os.MkdirAll(logsPath, os.ModePerm)
	if err != nil {
		return "", err
	}
	return logFile, nil
}

// GetNohupOutput
func GetNohupOutput(programType int) string {
	logFile, err := GetLogFile(programType)
	if err == nil {
		return logFile
	}
	return "/dev/null"
}

// fileWriterWithoutErr write func does not return err under any conditions
// To solve "Failed to write to log, write logs/chaosblade.log: no space left on device" err
type fileWriterWithoutErr struct {
	io.Writer
}

func (f *fileWriterWithoutErr) Write(b []byte) (n int, err error) {
	i, _ := f.Writer.Write(b)
	return i, nil
}
